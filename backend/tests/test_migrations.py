"""The initial migration (doc 03 task 8).

Two things are proven here: the migration builds the schema on an empty
database, and it builds *the same* schema the models declare - ``alembic check``
with no drift, run through ``compare_metadata`` so it fails a test rather than a
developer's command line.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.runtime.migration import MigrationContext

from alembic import command
from gaia.db.engine import sync_url
from gaia.db.models import Base

BACKEND = Path(__file__).resolve().parents[1]
ALEMBIC_INI = BACKEND / "alembic.ini"


@pytest.fixture
def database_url(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    """Point Alembic's env.py at a throwaway database through ``GAIA_DB_URL``."""
    url = f"sqlite+aiosqlite:///{tmp_path / 'migrated.sqlite'}"
    monkeypatch.setenv("GAIA_DB_URL", url)
    return url


@pytest.fixture
def alembic_config() -> Config:
    """The real ``alembic.ini``, so the test exercises the shipped configuration."""
    config = Config(str(ALEMBIC_INI))
    config.set_main_option("script_location", str(BACKEND / "alembic"))
    return config


@pytest.fixture
def engine(database_url: str) -> Iterator[sa.Engine]:
    """A synchronous engine on the migrated file, which is what Alembic uses."""
    sync_engine = sa.create_engine(sync_url(database_url))
    try:
        yield sync_engine
    finally:
        sync_engine.dispose()


def test_upgrade_creates_the_whole_schema(alembic_config: Config, engine: sa.Engine) -> None:
    """``make migrate`` on an empty database creates every table doc 03 lists."""
    command.upgrade(alembic_config, "head")

    tables = set(sa.inspect(engine).get_table_names())
    assert tables == set(Base.metadata.tables) | {"alembic_version"}
    assert "users" in tables  # not "user": reserved in PostgreSQL


def test_the_migration_matches_the_models(alembic_config: Config, engine: sa.Engine) -> None:
    """No drift: what the migration builds is what the models declare."""
    command.upgrade(alembic_config, "head")

    with engine.connect() as connection:
        context = MigrationContext.configure(connection, opts={"compare_type": True})
        assert compare_metadata(context, Base.metadata) == []


def test_downgrade_to_base_removes_everything(alembic_config: Config, engine: sa.Engine) -> None:
    """The initial revision is reversible, which keeps a bad release undoable."""
    command.upgrade(alembic_config, "head")
    command.downgrade(alembic_config, "base")

    assert set(sa.inspect(engine).get_table_names()) == {"alembic_version"}
