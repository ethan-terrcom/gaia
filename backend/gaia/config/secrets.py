"""Secret storage: the OS keychain, behind a narrow protocol.

Owned by doc 02. ARCHITECTURE section 7 keeps :class:`SecretStore` an interface
so a cloud secret manager can replace the keychain later without touching the
rest of Gaia.

Every value a store returns is registered with
:func:`gaia.security.redaction.register_secret`, so a key that reaches a log line
is masked even when some caller forgets it is a secret.
"""

from __future__ import annotations

from enum import StrEnum
from secrets import token_urlsafe
from typing import Protocol, runtime_checkable

import keyring
from keyring.errors import PasswordDeleteError
from pydantic import SecretStr

from gaia.security.redaction import register_secret

#: Keychain service name used for Gaia's own secrets.
DEFAULT_SERVICE = "gaia"

#: Entropy in the generated API token; ``token_urlsafe`` renders 32 bytes as 43
#: characters. Doc 04 generates and compares it.
API_TOKEN_BYTES = 32


class SecretName(StrEnum):
    """The secrets Gaia knows how to use. Nothing else can be stored."""

    ANTHROPIC_API_KEY = "ANTHROPIC_API_KEY"
    DEEPSEEK_API_KEY = "DEEPSEEK_API_KEY"
    # ruff's S105 ("hardcoded password") fires on these *names*. Both are keychain
    # entry names, never values, so the suppression cannot hide a credential.
    GITHUB_TOKEN = "GITHUB_TOKEN"  # noqa: S105 - an entry name, not a secret value
    GAIA_API_TOKEN = "GAIA_API_TOKEN"  # noqa: S105 - an entry name, not a secret value


class MissingSecretError(LookupError):
    """A required secret is not in the store. The message says how to fix it."""

    def __init__(self, name: SecretName) -> None:
        self.name = name
        super().__init__(f"{name.value} is not set. Run: gaia secrets set {name.value}")


@runtime_checkable
class SecretStore(Protocol):
    """Read and write short-lived credentials. See ARCHITECTURE section 7."""

    def get(self, name: SecretName) -> SecretStr | None:
        """Return the stored value, or ``None`` when it is not set."""
        ...

    def require(self, name: SecretName) -> SecretStr:
        """Return the stored value or raise :class:`MissingSecretError`."""
        ...

    def set(self, name: SecretName, value: str) -> None:
        """Store ``value``, replacing any previous one."""
        ...

    def delete(self, name: SecretName) -> bool:
        """Remove ``name``; ``False`` if it was not set.

        Deleting the credential does **not** un-register its value from the
        redaction registry. A traceback or a log record that captured the value
        before the deletion may not be rendered yet, so the value stays masked
        until the process exits; the registry is process-wide and cheap, and a
        leak is not recoverable while an extra masked string costs nothing.
        """
        ...


def _clean_value(value: str) -> str:
    """Strip the ends and reject empty values and embedded whitespace.

    A pasted key often carries a trailing newline; anything else that looks
    whitespace-separated is a mis-paste (two keys, a whole ``export`` line) and
    would fail confusingly later.
    """
    cleaned = value.strip()
    if not cleaned:
        raise ValueError("a secret value must not be empty")
    if any(character.isspace() for character in cleaned):
        raise ValueError("a secret value must not contain whitespace")
    return cleaned


def _as_secret(value: str) -> SecretStr:
    """Wrap a fetched value and register it for redaction."""
    register_secret(value)
    return SecretStr(value)


def _require(store: SecretStore, name: SecretName) -> SecretStr:
    value = store.get(name)
    if value is None:
        raise MissingSecretError(name)
    return value


class KeyringSecretStore:
    """A :class:`SecretStore` backed by the OS keychain through ``keyring``.

    The keychain "username" is the :class:`SecretName` value, which keeps the
    entries greppable in Keychain Access without revealing anything.
    """

    def __init__(self, service: str = DEFAULT_SERVICE) -> None:
        self.service = service

    def get(self, name: SecretName) -> SecretStr | None:
        value = keyring.get_password(self.service, name.value)
        return None if value is None else _as_secret(value)

    def require(self, name: SecretName) -> SecretStr:
        return _require(self, name)

    def set(self, name: SecretName, value: str) -> None:
        cleaned = _clean_value(value)
        keyring.set_password(self.service, name.value, cleaned)
        register_secret(cleaned)

    def delete(self, name: SecretName) -> bool:
        """Remove the keychain entry; the value stays redacted (see the protocol)."""
        if keyring.get_password(self.service, name.value) is None:
            return False
        try:
            keyring.delete_password(self.service, name.value)
        except PasswordDeleteError:
            # Some backends raise instead of returning when nothing matched.
            return False
        return True


class InMemorySecretStore:
    """A :class:`SecretStore` that lives in a dict, for tests and fakes."""

    def __init__(self) -> None:
        self._values: dict[SecretName, str] = {}

    def get(self, name: SecretName) -> SecretStr | None:
        value = self._values.get(name)
        return None if value is None else _as_secret(value)

    def require(self, name: SecretName) -> SecretStr:
        return _require(self, name)

    def set(self, name: SecretName, value: str) -> None:
        cleaned = _clean_value(value)
        self._values[name] = cleaned
        register_secret(cleaned)

    def delete(self, name: SecretName) -> bool:
        """Drop the stored value; it stays redacted (see the protocol)."""
        return self._values.pop(name, None) is not None


def get_secret_store() -> SecretStore:
    """Return the SecretStore the app and the CLI should use.

    One factory, so tests can substitute :class:`InMemorySecretStore` and later
    docs can substitute a cloud store (ARCHITECTURE section 7).
    """
    return KeyringSecretStore()


def ensure_api_token(store: SecretStore) -> SecretStr:
    """Return ``GAIA_API_TOKEN``, generating and storing one on first run.

    Doc 04 owns this entry: the API server and ``gaia ui`` both call it, so
    whichever runs first creates the token and later calls return the same one.
    Nobody types this secret, which is why ``gaia secrets set`` refuses the name -
    a hand-written token would be a token in a shell history.

    ``token_urlsafe(32)`` is 32 bytes of entropy in 43 URL-safe characters: long
    enough that guessing is hopeless, and safe to put in a URL fragment.
    """
    existing = store.get(SecretName.GAIA_API_TOKEN)
    if existing is not None:
        return existing
    store.set(SecretName.GAIA_API_TOKEN, token_urlsafe(API_TOKEN_BYTES))
    return store.require(SecretName.GAIA_API_TOKEN)
