"""The async engine, the session factory and SQLite's two pragmas.

Owned by doc 03. Everything above this module gets a session and never builds an
engine: the app (doc 04) creates one per process, Alembic builds a synchronous
one from the same URL (:func:`sync_url`), and tests build one per temporary
database.

The pragmas matter for correctness, not for speed:

- ``foreign_keys=ON``: SQLite ignores foreign keys unless asked, so without it
  every ``ondelete`` in :mod:`gaia.db.models` would be decoration.
- ``journal_mode=WAL``: readers do not block the writer, which is what makes a
  dashboard poll the API while a run is writing events.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import event
from sqlalchemy.engine.interfaces import DBAPIConnection
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import ConnectionPoolEntry

from gaia.config.settings import Settings

#: The async driver suffix doc 02 puts in ``Settings.db_url``.
ASYNC_SQLITE_DRIVER = "+aiosqlite"


def sync_url(url: str) -> str:
    """Drop the async driver, so Alembic can use the same URL synchronously.

    Alembic's default runner is synchronous, and ``sqlite3`` - the driver
    ``sqlite://`` selects - reads the same file. This keeps one source of truth
    for the database location (``Settings.db_url``) without an async migration
    runner.
    """
    return url.replace(ASYNC_SQLITE_DRIVER, "", 1)


def apply_sqlite_pragmas(
    dbapi_connection: DBAPIConnection, _record: ConnectionPoolEntry | None = None
) -> None:
    """Apply SQLite's pragmas to one DB-API connection.

    Registered as the engine's ``connect`` handler below. It is public (and
    takes the pool record optionally) because the handler runs inside a greenlet,
    which the test coverage tracer does not follow; a direct test is the only way
    to keep it covered.
    """
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA foreign_keys=ON")
    finally:
        cursor.close()


def create_engine(*, url: str, echo: bool = False) -> AsyncEngine:
    """Build an async engine for ``url``, with SQLite's pragmas when relevant."""
    async_engine = create_async_engine(url, echo=echo)
    if async_engine.dialect.name == "sqlite":
        # The recipe SQLAlchemy documents for async engines: listen on the
        # sync engine, which is where the DB-API connection is handed over.
        event.listen(async_engine.sync_engine, "connect", apply_sqlite_pragmas)
    return async_engine


def create_engine_for(settings: Settings) -> AsyncEngine:
    """Build the engine described by :class:`~gaia.config.settings.Settings`."""
    return create_engine(url=settings.db_url)


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """Return the session factory the app, services and tests share.

    ``expire_on_commit=False``: services return their rows after committing, and
    an expired row would cost a second round trip (or an ``await`` the caller
    cannot make) just to read a field it already had.
    """
    return async_sessionmaker(engine, expire_on_commit=False)


@asynccontextmanager
async def session_scope(
    factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncSession]:
    """Open a session, commit on success and roll back on any exception."""
    async with factory() as session:
        try:
            yield session
        except BaseException:
            await session.rollback()
            raise
        else:
            await session.commit()
