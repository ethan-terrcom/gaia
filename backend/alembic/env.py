"""Alembic's environment (doc 03 task 8).

Two decisions live here:

- **The URL comes from settings, not from ``alembic.ini``.** ``Settings.db_url``
  already follows ``GAIA_DATA_DIR`` (doc 02), so ``make migrate`` and the app can
  never disagree about which file the database is.
- **Migrations run synchronously.** The app uses ``sqlite+aiosqlite``, but
  Alembic's runner is synchronous; ``sync_url()`` drops the async driver, and
  ``sqlite3`` opens the same file.
"""

from __future__ import annotations

from logging.config import fileConfig
from pathlib import Path
from typing import Any

from alembic.autogenerate.api import AutogenContext
from sqlalchemy import engine_from_config, pool
from sqlalchemy.engine import make_url

from alembic import context
from gaia.config.settings import DATA_DIR_MODE, Settings
from gaia.db.engine import sync_url

# Importing the models is what gives autogenerate something to compare against.
from gaia.db.models import Base
from gaia.db.types import UtcDateTime

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = Base.metadata


def render_item(type_: str, obj: Any, autogen_context: AutogenContext) -> str | bool:
    """Render Gaia's own column type as ``UtcDateTime()`` plus its import.

    Without this, autogenerate writes ``gaia.db.types.UtcDateTime()`` and no
    import, so the migration fails on a fresh checkout.
    """
    if type_ == "type" and isinstance(obj, UtcDateTime):
        autogen_context.imports.add("from gaia.db.types import UtcDateTime")
        return "UtcDateTime()"
    return False


def database_url() -> str:
    """Return the database URL, creating the SQLite file's directory if needed."""
    settings = Settings()
    url = sync_url(settings.db_url)
    database = make_url(url).database
    if database and database != ":memory:":
        directory = Path(database).expanduser().parent
        directory.mkdir(mode=DATA_DIR_MODE, parents=True, exist_ok=True)
        if directory == settings.data_dir:
            # ``make migrate`` can be the first thing that runs, so it applies
            # doc 02's 0o700 rule to ~/.gaia itself (mkdir's mode is umasked).
            directory.chmod(DATA_DIR_MODE)
    return url


def run_migrations_offline() -> None:
    """Write the migration SQL to stdout instead of running it."""
    context.configure(
        url=database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=True,
        render_item=render_item,
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run the migrations against the configured database."""
    section = config.get_section(config.config_ini_section, {})
    section["sqlalchemy.url"] = database_url()
    connectable = engine_from_config(
        section,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=True,
            render_item=render_item,
            compare_type=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
