"""repository allowed commands

Owner-registered command prefixes an agent may run without asking for approval,
as a JSON list of argv lists. ``NULL`` means none.

Revision ID: 0006
Revises: 0005
Create Date: 2026-11-16 18:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("repository", schema=None) as batch_op:
        batch_op.add_column(sa.Column("allowed_commands", sa.JSON(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("repository", schema=None) as batch_op:
        batch_op.drop_column("allowed_commands")
