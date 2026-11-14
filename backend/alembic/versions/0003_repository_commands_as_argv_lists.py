"""repository commands as argv lists

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-13 23:59:59.000000

Doc 06 changes ``repository.setup_command`` and ``repository.test_command`` from
single text values to JSON argv lists. SQLite cannot change a column type in
place, so the table is rebuilt in batch mode and the existing text values are
backfilled as one-item JSON arrays (``NULL`` when the old value was empty or
null). The downgrade reads the first argv item back into the text column; a
multi-item list therefore cannot round-trip, which is expected when moving from
a list to a single string.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa

from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _backfill_value(value: Any) -> str | None:
    """Convert an old text value into a JSON argv-list string."""
    if value is None:
        return None
    text = str(value)
    if text == "":
        return None
    return json.dumps([text])


def _first_text(value: Any) -> str | None:
    """Convert a JSON argv list back into the old single text value."""
    if value is None:
        return None
    if isinstance(value, list):
        return value[0] if value else None
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (TypeError, json.JSONDecodeError):
            return value or None
        if isinstance(parsed, list):
            return parsed[0] if parsed else None
    return str(value) or None


def upgrade() -> None:
    with op.batch_alter_table("repository", schema=None) as batch_op:
        batch_op.alter_column(
            "setup_command",
            existing_type=sa.Text(),
            type_=sa.JSON(),
            existing_nullable=True,
            nullable=True,
        )
        batch_op.alter_column(
            "test_command",
            existing_type=sa.Text(),
            type_=sa.JSON(),
            existing_nullable=True,
            nullable=True,
        )

    connection = op.get_bind()
    rows = connection.execute(
        sa.text("SELECT id, setup_command, test_command FROM repository")
    ).mappings()
    for row in rows:
        connection.execute(
            sa.text(
                "UPDATE repository SET setup_command = :setup, test_command = :test WHERE id = :id"
            ),
            {
                "setup": _backfill_value(row["setup_command"]),
                "test": _backfill_value(row["test_command"]),
                "id": row["id"],
            },
        )


def downgrade() -> None:
    connection = op.get_bind()
    rows = connection.execute(
        sa.text("SELECT id, setup_command, test_command FROM repository")
    ).mappings()
    for row in rows:
        connection.execute(
            sa.text(
                "UPDATE repository SET setup_command = :setup, test_command = :test WHERE id = :id"
            ),
            {
                "setup": _first_text(row["setup_command"]),
                "test": _first_text(row["test_command"]),
                "id": row["id"],
            },
        )

    with op.batch_alter_table("repository", schema=None) as batch_op:
        batch_op.alter_column(
            "setup_command",
            existing_type=sa.JSON(),
            type_=sa.Text(),
            existing_nullable=True,
            nullable=True,
        )
        batch_op.alter_column(
            "test_command",
            existing_type=sa.JSON(),
            type_=sa.Text(),
            existing_nullable=True,
            nullable=True,
        )
