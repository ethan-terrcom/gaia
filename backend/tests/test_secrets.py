"""Tests for ``gaia.config.secrets`` (doc 02, task 3).

The autouse ``fake_keyring`` fixture in ``conftest.py`` makes ``KeyringSecretStore``
run against an in-memory backend, so nothing here can reach the real keychain.
"""

from __future__ import annotations

import keyring
import pytest
from keyring.errors import PasswordDeleteError

from gaia.config.secrets import (
    DEFAULT_SERVICE,
    InMemorySecretStore,
    KeyringSecretStore,
    MissingSecretError,
    SecretName,
    SecretStore,
    ensure_api_token,
    get_secret_store,
)
from gaia.security.redaction import REDACTED, redact

DUMMY_ANTHROPIC = "dummy" + "-anthropic-value"
DUMMY_GITHUB = "dummy" + "-github-value"
DUMMY_TOKEN = "dummy" + "-api-token-value"


@pytest.fixture(params=["in_memory", "keyring"])
def store(request: pytest.FixtureRequest) -> SecretStore:
    """Both implementations must satisfy the same protocol and behaviour."""
    if request.param == "in_memory":
        return InMemorySecretStore()
    return KeyringSecretStore()


@pytest.fixture
def keyring_store() -> KeyringSecretStore:
    return KeyringSecretStore()


def test_the_known_secret_names_are_exactly_this_set() -> None:
    assert [name.value for name in SecretName] == [
        "ANTHROPIC_API_KEY",
        "DEEPSEEK_API_KEY",
        "GITHUB_TOKEN",
        "GAIA_API_TOKEN",
    ]


def test_both_stores_implement_the_protocol() -> None:
    assert isinstance(InMemorySecretStore(), SecretStore)
    assert isinstance(KeyringSecretStore(), SecretStore)


def test_the_factory_returns_the_keyring_store() -> None:
    assert isinstance(get_secret_store(), KeyringSecretStore)


def test_set_then_get_then_delete_round_trips(store: SecretStore) -> None:
    assert store.get(SecretName.ANTHROPIC_API_KEY) is None

    store.set(SecretName.ANTHROPIC_API_KEY, DUMMY_ANTHROPIC)

    fetched = store.get(SecretName.ANTHROPIC_API_KEY)
    assert fetched is not None
    assert fetched.get_secret_value() == DUMMY_ANTHROPIC
    assert store.delete(SecretName.ANTHROPIC_API_KEY) is True
    assert store.get(SecretName.ANTHROPIC_API_KEY) is None


def test_delete_of_an_unset_name_returns_false(store: SecretStore) -> None:
    assert store.delete(SecretName.GITHUB_TOKEN) is False


def test_set_overwrites_the_previous_value(store: SecretStore) -> None:
    store.set(SecretName.DEEPSEEK_API_KEY, "dummy" + "-first-value")
    store.set(SecretName.DEEPSEEK_API_KEY, "dummy" + "-second-value")

    fetched = store.require(SecretName.DEEPSEEK_API_KEY)

    assert fetched.get_secret_value() == "dummy" + "-second-value"


def test_require_returns_the_value(store: SecretStore) -> None:
    store.set(SecretName.GITHUB_TOKEN, DUMMY_GITHUB)

    assert store.require(SecretName.GITHUB_TOKEN).get_secret_value() == DUMMY_GITHUB


def test_require_on_a_missing_secret_names_the_fix(store: SecretStore) -> None:
    with pytest.raises(MissingSecretError) as excinfo:
        store.require(SecretName.ANTHROPIC_API_KEY)

    assert str(excinfo.value) == (
        "ANTHROPIC_API_KEY is not set. Run: gaia secrets set ANTHROPIC_API_KEY"
    )
    assert excinfo.value.name is SecretName.ANTHROPIC_API_KEY


@pytest.mark.parametrize("value", ["", "   ", "\n", "\t \n"])
def test_an_empty_value_is_rejected(store: SecretStore, value: str) -> None:
    with pytest.raises(ValueError, match="empty"):
        store.set(SecretName.ANTHROPIC_API_KEY, value)

    assert store.get(SecretName.ANTHROPIC_API_KEY) is None


@pytest.mark.parametrize("value", ["dummy value", "dummy\nvalue", "dummy-key extra"])
def test_a_value_with_embedded_whitespace_is_rejected(store: SecretStore, value: str) -> None:
    with pytest.raises(ValueError, match="whitespace"):
        store.set(SecretName.ANTHROPIC_API_KEY, value)

    assert store.get(SecretName.ANTHROPIC_API_KEY) is None


def test_surrounding_whitespace_is_stripped(store: SecretStore) -> None:
    store.set(SecretName.GITHUB_TOKEN, f"  {DUMMY_GITHUB}\n")

    assert store.require(SecretName.GITHUB_TOKEN).get_secret_value() == DUMMY_GITHUB


def test_a_secret_never_prints_itself(store: SecretStore) -> None:
    store.set(SecretName.ANTHROPIC_API_KEY, DUMMY_ANTHROPIC)

    fetched = store.require(SecretName.ANTHROPIC_API_KEY)

    assert DUMMY_ANTHROPIC not in repr(fetched)
    assert DUMMY_ANTHROPIC not in str(fetched)


def test_a_fetched_value_is_redacted_from_then_on(store: SecretStore) -> None:
    store.set(SecretName.ANTHROPIC_API_KEY, DUMMY_ANTHROPIC)

    fetched = store.require(SecretName.ANTHROPIC_API_KEY)

    assert fetched.get_secret_value() == DUMMY_ANTHROPIC
    assert redact(f"log line {DUMMY_ANTHROPIC}") == f"log line {REDACTED}"


def test_deleting_a_secret_does_not_un_redact_it(store: SecretStore) -> None:
    """Deleting a credential must not make earlier log text printable again.

    ``delete()`` removes the value from the store but deliberately leaves it in
    the redaction registry: a traceback or a log record built before the deletion
    may still be waiting to be rendered, and the registry dies with the process.
    The dummy value matches none of the token patterns, so it is masked only
    because the store registered it.
    """
    store.set(SecretName.ANTHROPIC_API_KEY, DUMMY_ANTHROPIC)

    assert store.delete(SecretName.ANTHROPIC_API_KEY) is True

    assert redact(f"log line {DUMMY_ANTHROPIC}") == f"log line {REDACTED}"


def test_the_keyring_store_uses_the_enum_value_as_the_username(
    keyring_store: KeyringSecretStore,
) -> None:
    keyring_store.set(SecretName.GITHUB_TOKEN, DUMMY_GITHUB)

    assert keyring.get_password(DEFAULT_SERVICE, "GITHUB_TOKEN") == DUMMY_GITHUB


def test_the_keyring_service_name_can_be_overridden() -> None:
    store = KeyringSecretStore(service="gaia-test")

    store.set(SecretName.GITHUB_TOKEN, DUMMY_GITHUB)

    assert keyring.get_password("gaia-test", "GITHUB_TOKEN") == DUMMY_GITHUB
    assert KeyringSecretStore().get(SecretName.GITHUB_TOKEN) is None


def test_the_keyring_store_registers_values_fetched_from_the_backend() -> None:
    # Written straight to the keychain, so get() is what has to register the value.
    keyring.set_password(DEFAULT_SERVICE, SecretName.ANTHROPIC_API_KEY.value, DUMMY_ANTHROPIC)

    fetched = KeyringSecretStore().get(SecretName.ANTHROPIC_API_KEY)

    assert fetched is not None
    assert DUMMY_ANTHROPIC not in redact(f"log line {DUMMY_ANTHROPIC}")


def test_a_backend_that_refuses_to_delete_reports_false(
    keyring_store: KeyringSecretStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    keyring_store.set(SecretName.GITHUB_TOKEN, DUMMY_GITHUB)

    def refuse(service: str, username: str) -> None:
        raise PasswordDeleteError("cannot delete")

    monkeypatch.setattr(keyring.get_keyring(), "delete_password", refuse)

    assert keyring_store.delete(SecretName.GITHUB_TOKEN) is False


def test_ensure_api_token_generates_and_stores_one(store: SecretStore) -> None:
    """Doc 04: first run creates the token, so nothing can be left unauthenticated."""
    token = ensure_api_token(store)

    stored = store.require(SecretName.GAIA_API_TOKEN)
    assert stored.get_secret_value() == token.get_secret_value()
    # token_urlsafe(32) is 43 URL-safe characters.
    assert len(token.get_secret_value()) == 43


def test_ensure_api_token_is_stable(store: SecretStore) -> None:
    """Two generations would log the UI out on every restart."""
    assert ensure_api_token(store).get_secret_value() == ensure_api_token(store).get_secret_value()


def test_ensure_api_token_keeps_a_value_that_already_exists(store: SecretStore) -> None:
    store.set(SecretName.GAIA_API_TOKEN, DUMMY_TOKEN)

    assert ensure_api_token(store).get_secret_value() == DUMMY_TOKEN


def test_a_generated_token_is_redacted_in_logs(store: SecretStore) -> None:
    """A token in a URL fragment must not survive a log line."""
    token = ensure_api_token(store).get_secret_value()

    assert token not in redact(f"GET /?token={token}")
