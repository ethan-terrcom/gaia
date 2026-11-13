"""Who is calling: the ``AuthProvider`` seam and the local bearer token.

Owned by doc 04 (task 2). ARCHITECTURE section 7 asks for an interface here, so
v1 ships :class:`LocalTokenAuth` - one generated token, one local owner - and a
later package can add OIDC without touching a route: everything above this
module asks for a :class:`Principal`, never for a token.

The token itself is generated and stored by
:func:`gaia.config.secrets.ensure_api_token` on first run, so the CLI can hand
it to the UI without importing FastAPI. This module only compares it, and it
always compares in constant time: a token check that returns early leaks the
length of the shared prefix to anyone who can time a request.
"""

from __future__ import annotations

import hmac
from typing import Final, Protocol, runtime_checkable

from fastapi import Request
from fastapi.security import HTTPBearer
from pydantic import BaseModel, ConfigDict

from gaia.config.secrets import SecretName, SecretStore

#: The challenge word in ``Authorization`` and in ``WWW-Authenticate`` (RFC 6750).
BEARER: Final = "bearer"

#: Registered in the OpenAPI document so a generated client knows the scheme.
#: ``auto_error=False``: this instance never rejects, :func:`deps.get_principal`
#: does the check, so there is exactly one place for it to be wrong.
bearer_scheme: Final = HTTPBearer(auto_error=False, scheme_name="bearer", bearerFormat=None)


class Principal(BaseModel):
    """The authenticated caller, and the workspace every query is scoped to."""

    model_config = ConfigDict(frozen=True)

    user_id: str
    workspace_id: str


class AuthenticationError(Exception):
    """The request did not carry a valid bearer token.

    The message is safe to show: it never contains the presented value or the
    expected one.
    """


@runtime_checkable
class AuthProvider(Protocol):
    """Turn a request into a :class:`Principal`, or raise.

    ``LocalTokenAuth`` implements it today; OIDC later. Raising
    :class:`AuthenticationError` is the whole contract - the 401 envelope is
    built by :mod:`gaia.api.errors`.
    """

    def authenticate(self, request: Request) -> Principal:
        """Return the caller, or raise :class:`AuthenticationError`."""
        ...


def bearer_token(request: Request) -> str | None:
    """Return the token from ``Authorization: Bearer <token>``, if there is one.

    The scheme name is case-insensitive (RFC 7235 section 2.1) and the token is
    stripped, because a trailing newline survives some HTTP clients. Anything
    else - no header, another scheme, an empty value - is "no token".
    """
    header = request.headers.get("authorization")
    if header is None:
        return None
    scheme, _, credentials = header.partition(" ")
    if scheme.lower() != BEARER:
        return None
    token = credentials.strip()
    return token or None


class LocalTokenAuth:
    """Single-user local auth: the token in the keychain, the seeded owner.

    The principal is fixed at startup from the seeded workspace and user, since
    a local install has exactly one of each. A second user is an OIDC concern,
    not a token concern.
    """

    def __init__(self, store: SecretStore, principal: Principal) -> None:
        self.store = store
        self.principal = principal

    def authenticate(self, request: Request) -> Principal:
        """Compare the presented token with the stored one, in constant time."""
        presented = bearer_token(request)
        if presented is None:
            raise AuthenticationError("a bearer token is required")
        expected = self.store.get(SecretName.GAIA_API_TOKEN)
        if expected is None:
            # The lifespan generates the token before the first request; reaching
            # this means it was deleted while the server ran.
            raise AuthenticationError("no API token is configured; restart the server")
        # Compare bytes: `compare_digest` raises TypeError on non-ASCII `str`,
        # and an arbitrary header value is not required to be ASCII.
        if not hmac.compare_digest(presented.encode(), expected.get_secret_value().encode()):
            raise AuthenticationError("the bearer token is not valid")
        return self.principal
