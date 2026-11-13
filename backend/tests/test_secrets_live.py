"""Opt-in smoke test against the developer's real keychain (doc 02, task 3).

Marked ``live``, so it never runs in CI (``addopts`` selects ``-m "not live"``).
Run it by hand with ``uv run pytest -m live``. It writes to a throwaway service
name, so no real credential is read, written or deleted.
"""

from __future__ import annotations

import uuid

import keyring
import pytest
from keyring.backends.fail import Keyring as FailKeyring

from gaia.config.secrets import KeyringSecretStore, SecretName

pytestmark = pytest.mark.live


@pytest.fixture(autouse=True)
def fake_keyring() -> None:
    """Override the in-memory backend from conftest: this module wants the real one."""


@pytest.fixture
def live_store() -> KeyringSecretStore:
    if isinstance(keyring.get_keyring(), FailKeyring):
        pytest.skip("no keyring backend is available on this machine")
    return KeyringSecretStore(service=f"gaia-test-{uuid.uuid4()}")


def test_the_real_keychain_round_trips(live_store: KeyringSecretStore) -> None:
    value = "dummy" + "-live-value"

    live_store.set(SecretName.GITHUB_TOKEN, value)
    try:
        fetched = live_store.require(SecretName.GITHUB_TOKEN)
        assert fetched.get_secret_value() == value
    finally:
        assert live_store.delete(SecretName.GITHUB_TOKEN) is True

    assert live_store.get(SecretName.GITHUB_TOKEN) is None
