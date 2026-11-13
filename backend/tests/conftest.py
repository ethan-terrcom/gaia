"""Shared test fixtures.

Doc 02 requires that no test touches the real keychain or ``~/.gaia``:

- an in-memory ``keyring`` backend is installed for every test, because CI runs
  on Linux, which has no keychain at all;
- ``GAIA_*`` variables from the developer's shell are removed, so a stray
  ``GAIA_DATA_DIR`` cannot point a test at the real ``~/.gaia``;
- the redaction registry and the ``get_settings()`` cache are process-wide
  state, so both are reset around every test.

Doc 03 adds one more rule: every database test gets its own SQLite **file** in
``tmp_path``. A file rather than ``:memory:``, because each connection to
``:memory:`` opens a separate empty database, and because the WAL pragma only
means anything for a real file.

Doc 04 adds the :func:`api` fixture: the real app on the test's database, with an
in-memory secret store, entered through its own lifespan. It uses
``httpx.AsyncClient`` over an ASGI transport rather than FastAPI's ``TestClient``,
because ``starlette.testclient`` imports a deprecated ``httpx`` shim (a warning,
which this project turns into an error) and because doc 06 tests the API the same
way.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from pathlib import Path

import httpx
import keyring
import pytest
from fastapi import FastAPI
from keyring.backend import KeyringBackend
from keyring.errors import PasswordDeleteError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from gaia.api import app as app_module
from gaia.api.app import create_app
from gaia.config.secrets import InMemorySecretStore, SecretName
from gaia.config.settings import Settings, get_settings
from gaia.db.engine import create_engine, create_session_factory
from gaia.db.models import Base
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


@dataclass(frozen=True)
class Database:
    """A throwaway SQLite database, its engine and its session factory."""

    engine: AsyncEngine
    sessions: async_sessionmaker[AsyncSession]
    url: str


@pytest.fixture
async def database(tmp_path: Path) -> AsyncIterator[Database]:
    """Create an empty schema in ``tmp_path`` and tear it down afterwards.

    Tests build their schema with ``create_all`` on purpose: the Alembic
    migration is exercised by ``tests/test_migrations.py``, which also proves the
    two agree.
    """
    url = f"sqlite+aiosqlite:///{tmp_path / 'gaia-test.sqlite'}"
    engine = create_engine(url=url)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    try:
        yield Database(engine=engine, sessions=create_session_factory(engine), url=url)
    finally:
        await engine.dispose()


@pytest.fixture
async def session(database: Database) -> AsyncIterator[AsyncSession]:
    """A session on the test's database (doc 03)."""
    async with database.sessions() as db_session:
        yield db_session


#: Every API test talks to this origin, because ``TrustedHostMiddleware`` accepts
#: only ``127.0.0.1`` and ``localhost`` in the ``Host`` header (doc 04 task 3).
API_BASE_URL = "http://127.0.0.1:8765"


@dataclass(frozen=True)
class Api:
    """A running app on a throwaway database, plus the client that talks to it."""

    client: httpx.AsyncClient
    app: FastAPI
    settings: Settings
    store: InMemorySecretStore

    @property
    def token(self) -> str:
        """The API token the server generated, as it stored it."""
        return self.store.require(SecretName.GAIA_API_TOKEN).get_secret_value()

    @property
    def auth(self) -> dict[str, str]:
        """Headers that authenticate as the seeded local owner."""
        return {"Authorization": f"Bearer {self.token}"}


@pytest.fixture
async def api(
    tmp_path: Path,
    database: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[Api]:
    """Start the real app on the test's database and an in-memory secret store.

    The lifespan is entered directly: it is what opens the engine, seeds the
    workspace and generates the token, so testing it is the point (doc 04 task 1).
    """
    store = InMemorySecretStore()
    monkeypatch.setattr(app_module, "get_secret_store", lambda: store)
    settings = Settings(data_dir=tmp_path / "gaia-data", db_url=database.url)
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url=API_BASE_URL) as client:
            yield Api(client=client, app=app, settings=settings, store=store)
