"""The engine, its SQLite pragmas and the session scope (doc 03).

The pragmas are correctness features, not tuning: without ``foreign_keys=ON``
SQLite silently ignores every ``ondelete`` in the schema.
"""

from __future__ import annotations

from pathlib import Path
from typing import cast

import pytest
from sqlalchemy import event as sqlalchemy_event
from sqlalchemy import select, text
from sqlalchemy.engine.interfaces import DBAPIConnection
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from gaia.config.settings import Settings
from gaia.db import engine as engine_module
from gaia.db.engine import apply_sqlite_pragmas, create_engine_for, session_scope, sync_url
from gaia.db.models import Ticket, Workspace
from gaia.db.repo import ScopedRepo
from tests.conftest import Database
from tests.db_builders import make_workspace


def test_sync_url_drops_the_async_driver() -> None:
    """Alembic opens the same file with ``sqlite://`` and ``sqlite3``."""
    assert sync_url("sqlite+aiosqlite:///tmp/gaia.sqlite") == "sqlite:///tmp/gaia.sqlite"


def test_sync_url_leaves_other_urls_alone() -> None:
    """PostgreSQL URLs pass through untouched: the swap is SQLite-only."""
    assert sync_url("postgresql+psycopg://u@h/db") == "postgresql+psycopg://u@h/db"


async def test_a_sqlite_connection_has_wal_and_foreign_keys(database: Database) -> None:
    """Both pragmas are applied to every new connection."""
    async with database.engine.connect() as connection:
        journal_mode = (await connection.execute(text("PRAGMA journal_mode"))).scalar()
        foreign_keys = (await connection.execute(text("PRAGMA foreign_keys"))).scalar()
    assert journal_mode == "wal"
    assert foreign_keys == 1


async def test_foreign_keys_are_enforced(session: AsyncSession) -> None:
    """With the pragma on, a dangling foreign key is a database error."""
    workspace = await make_workspace(session)
    tickets = ScopedRepo(session, Ticket, workspace.id)
    with pytest.raises(IntegrityError, match="FOREIGN KEY"):
        await tickets.create(repo_id="no-such-repository", title="Orphan")


def test_the_pragma_handler_runs_both_pragmas_and_closes_the_cursor() -> None:
    """Unit level: the handler issues the two pragmas and never leaks a cursor."""

    class FakeCursor:
        def __init__(self) -> None:
            self.statements: list[str] = []
            self.closed = False

        def execute(self, statement: str) -> None:
            self.statements.append(statement)

        def close(self) -> None:
            self.closed = True

    class FakeConnection:
        def __init__(self) -> None:
            self.fake_cursor = FakeCursor()

        def cursor(self) -> FakeCursor:
            return self.fake_cursor

    connection = FakeConnection()
    apply_sqlite_pragmas(cast("DBAPIConnection", connection))

    assert connection.fake_cursor.statements == [
        "PRAGMA journal_mode=WAL",
        "PRAGMA foreign_keys=ON",
    ]
    assert connection.fake_cursor.closed


def test_a_non_sqlite_engine_gets_no_pragma_listener(monkeypatch: pytest.MonkeyPatch) -> None:
    """Only SQLite needs the pragmas; another dialect must not receive them."""
    listened: list[object] = []

    class FakeDialect:
        name = "postgresql"

    class FakeEngine:
        dialect = FakeDialect()
        sync_engine = object()

    monkeypatch.setattr(engine_module, "create_async_engine", lambda *args, **kwargs: FakeEngine())
    # The same module object engine_module holds, patched at its source so mypy
    # and ruff both accept the call.
    monkeypatch.setattr(sqlalchemy_event, "listen", lambda *args: listened.append(args))

    engine_module.create_engine(url="postgresql+psycopg://u@h/db")
    assert listened == []


async def test_create_engine_for_follows_settings(tmp_path: Path) -> None:
    """The factory takes the URL from ``Settings``, so app and CLI agree."""
    engine = create_engine_for(Settings(data_dir=tmp_path))
    try:
        assert engine.url.database == f"{tmp_path}/gaia.sqlite"
    finally:
        await engine.dispose()


async def test_session_scope_commits_on_success(database: Database) -> None:
    """A successful scope is committed, so a later session sees the row."""
    async with session_scope(database.sessions) as scoped:
        workspace = await make_workspace(scoped, slug="committed")

    async with database.sessions() as fresh:
        assert await fresh.get(Workspace, workspace.id) is not None


async def _boom_mid_scope(database: Database) -> None:
    """Write a row inside a scope, then fail: the scope must undo the write."""
    async with session_scope(database.sessions) as scoped:
        await make_workspace(scoped, slug="rolled-back")
        raise RuntimeError("boom")


async def test_session_scope_rolls_back_on_error(database: Database) -> None:
    """An exception inside the scope leaves nothing behind."""
    with pytest.raises(RuntimeError, match="boom"):
        await _boom_mid_scope(database)

    async with database.sessions() as fresh:
        assert list((await fresh.scalars(select(Workspace))).all()) == []
