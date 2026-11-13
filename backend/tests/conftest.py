"""Shared test fixtures.

Doc 02 requires that no test touches the real keychain or ``~/.gaia``:

- an in-memory ``keyring`` backend is installed for every test, because CI runs
  on Linux, which has no keychain at all;
- ``GAIA_*`` variables from the developer's shell are removed, so a stray
  ``GAIA_DATA_DIR`` cannot point a test at the real ``~/.gaia``;
- the redaction registry and the ``get_settings()`` cache are process-wide
  state, so both are reset around every test.
"""

from __future__ import annotations

import os
from collections.abc import Iterator

import keyring
import pytest
from keyring.backend import KeyringBackend
from keyring.errors import PasswordDeleteError

from gaia.config.settings import get_settings
from gaia.security import redaction


class MemoryKeyringBackend(KeyringBackend):
    """A keychain that lives in a dict and never leaves the process."""

    priority = 1

    def __init__(self) -> None:
        # Deliberately not calling KeyringBackend.__init__: it only reads
        # KEYRING_PROPERTY_* from the environment, which a fake does not need.
        self.values: dict[tuple[str, str], str] = {}

    def get_password(self, service: str, username: str) -> str | None:
        return self.values.get((service, username))

    def set_password(self, service: str, username: str, password: str) -> None:
        self.values[(service, username)] = password

    def delete_password(self, service: str, username: str) -> None:
        try:
            del self.values[(service, username)]
        except KeyError:
            raise PasswordDeleteError(f"{username} is not stored for {service}") from None


@pytest.fixture(autouse=True)
def fake_keyring() -> Iterator[MemoryKeyringBackend]:
    """Route every ``keyring`` call at an in-memory backend for one test."""
    previous = keyring.get_keyring()
    backend = MemoryKeyringBackend()
    keyring.set_keyring(backend)
    try:
        yield backend
    finally:
        keyring.set_keyring(previous)


@pytest.fixture(autouse=True)
def clean_gaia_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove the developer's ``GAIA_*`` variables; tests set what they need."""
    for name in list(os.environ):
        if name.startswith("GAIA_"):
            monkeypatch.delenv(name)


@pytest.fixture(autouse=True)
def reset_process_state() -> Iterator[None]:
    """Clear the redaction registry and the settings cache around every test."""
    redaction._reset_for_tests()
    get_settings.cache_clear()
    try:
        yield
    finally:
        redaction._reset_for_tests()
        get_settings.cache_clear()
