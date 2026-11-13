"""Tests for ``gaia.api.auth``: the bearer token and the ``AuthProvider`` seam.

Doc 04 task 2. There is no real keychain here: the autouse ``fake_keyring``
fixture in ``conftest.py`` installs an in-memory backend, and the store used
below is :class:`~gaia.config.secrets.InMemorySecretStore`.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError
from starlette.requests import Request
from starlette.types import Scope

from gaia.api.auth import (
    AuthenticationError,
    AuthProvider,
    LocalTokenAuth,
    Principal,
    bearer_token,
)
from gaia.config.secrets import InMemorySecretStore, SecretName

TOKEN = "token" + "-for-the-tests"
OTHER_TOKEN = "other" + "-token-for-the-tests"
PRINCIPAL = Principal(user_id="user-1", workspace_id="workspace-1")


def make_request(*, authorization: str | None = None) -> Request:
    """Build a bare request with just the headers a token check reads."""
    headers = [] if authorization is None else [(b"authorization", authorization.encode())]
    scope: Scope = {"type": "http", "headers": headers}
    return Request(scope)


@pytest.fixture
def store() -> InMemorySecretStore:
    """A store holding the token the server would have generated."""
    in_memory = InMemorySecretStore()
    in_memory.set(SecretName.GAIA_API_TOKEN, TOKEN)
    return in_memory


@pytest.fixture
def auth(store: InMemorySecretStore) -> LocalTokenAuth:
    return LocalTokenAuth(store, PRINCIPAL)


def test_the_local_provider_satisfies_the_protocol(auth: LocalTokenAuth) -> None:
    assert isinstance(auth, AuthProvider)


def test_bearer_token_reads_the_header() -> None:
    assert bearer_token(make_request(authorization=f"Bearer {TOKEN}")) == TOKEN


def test_bearer_token_accepts_any_scheme_case() -> None:
    """RFC 7235 makes the scheme name case-insensitive."""
    assert bearer_token(make_request(authorization=f"bearer {TOKEN}")) == TOKEN
    assert bearer_token(make_request(authorization=f"BEARER {TOKEN}")) == TOKEN


def test_bearer_token_strips_surrounding_whitespace() -> None:
    assert bearer_token(make_request(authorization=f"Bearer  {TOKEN} ")) == TOKEN


def test_bearer_token_is_none_without_a_header() -> None:
    assert bearer_token(make_request()) is None


def test_bearer_token_is_none_for_another_scheme() -> None:
    assert bearer_token(make_request(authorization=f"Basic {TOKEN}")) is None


def test_bearer_token_is_none_for_an_empty_value() -> None:
    assert bearer_token(make_request(authorization="Bearer ")) is None


def test_a_valid_token_returns_the_principal(auth: LocalTokenAuth) -> None:
    assert auth.authenticate(make_request(authorization=f"Bearer {TOKEN}")) == PRINCIPAL


def test_a_missing_token_is_rejected(auth: LocalTokenAuth) -> None:
    with pytest.raises(AuthenticationError, match="required"):
        auth.authenticate(make_request())


def test_a_wrong_token_is_rejected(auth: LocalTokenAuth) -> None:
    with pytest.raises(AuthenticationError, match="not valid"):
        auth.authenticate(make_request(authorization=f"Bearer {OTHER_TOKEN}"))


def test_a_token_that_is_a_prefix_of_the_real_one_is_rejected(auth: LocalTokenAuth) -> None:
    """The comparison covers the whole value, not just a shared prefix."""
    with pytest.raises(AuthenticationError):
        auth.authenticate(make_request(authorization=f"Bearer {TOKEN[:10]}"))


def test_a_non_ascii_token_is_rejected_not_a_crash(auth: LocalTokenAuth) -> None:
    """``hmac.compare_digest`` raises ``TypeError`` on non-ASCII ``str``.

    The comparison runs on bytes on purpose: a header value is arbitrary text, and
    a ``TypeError`` here would be a 500 where a 401 belongs.
    """
    with pytest.raises(AuthenticationError):
        auth.authenticate(make_request(authorization="Bearer tökén-tökén-tökén"))


def test_no_configured_token_is_rejected_with_a_different_message() -> None:
    """The token is generated at startup; deleting it mid-run must not crash."""
    auth = LocalTokenAuth(InMemorySecretStore(), PRINCIPAL)

    with pytest.raises(AuthenticationError, match="restart the server"):
        auth.authenticate(make_request(authorization=f"Bearer {TOKEN}"))


def test_the_rejection_never_echoes_the_presented_token(auth: LocalTokenAuth) -> None:
    with pytest.raises(AuthenticationError) as excinfo:
        auth.authenticate(make_request(authorization=f"Bearer {OTHER_TOKEN}"))

    assert OTHER_TOKEN not in str(excinfo.value)


def test_the_principal_is_frozen() -> None:
    """A route cannot change the workspace it was scoped to."""
    with pytest.raises(ValidationError, match="frozen"):
        # mypy sees the frozen field as read-only; making the assignment *is* the
        # test, so the ignore is the assertion's price.
        PRINCIPAL.workspace_id = "workspace-2"  # type: ignore[misc]
