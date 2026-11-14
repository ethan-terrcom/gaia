"""worktree git_dir and approval idempotency key

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-13 22:54:51.422924
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("approval", schema=None) as batch_op:
        # Added nullable first so existing rows can be backfilled; SQLite cannot
        # add a non-null column to a table that already has rows.
        batch_op.add_column(sa.Column("idempotency_key", sa.String(length=200), nullable=True))
    op.execute(sa.text("UPDATE approval SET idempotency_key = id"))
    with op.batch_alter_table("approval", schema=None) as batch_op:
        batch_op.alter_column(
            "idempotency_key", existing_type=sa.String(length=200), nullable=False
        )
        batch_op.create_unique_constraint(
            batch_op.f("uq_approval_run_id_idempotency_key"), ["run_id", "idempotency_key"]
        )

    with op.batch_alter_table("worktree", schema=None) as batch_op:
        batch_op.add_column(sa.Column("git_dir", sa.String(length=4096), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("worktree", schema=None) as batch_op:
        batch_op.drop_column("git_dir")

    with op.batch_alter_table("approval", schema=None) as batch_op:
        batch_op.drop_constraint(batch_op.f("uq_approval_run_id_idempotency_key"), type_="unique")
        batch_op.drop_column("idempotency_key")
