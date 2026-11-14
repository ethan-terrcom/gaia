"""run tokens

Doc 07b adds ``run_token``: a short-lived, hashed credential that lets one
runtime reach the LLM gateway. ``run_id`` cascades with the run;
``run_step_id`` and ``agent_id`` survive as ``NULL`` when their parent is
deleted. ``token_hash`` is the SHA-256 hex digest of the plaintext token.

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-14 07:06:42.446610
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from gaia.db.types import UtcDateTime

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "run_token",
        sa.Column("run_id", sa.String(length=36), nullable=False),
        sa.Column("run_step_id", sa.String(length=36), nullable=True),
        sa.Column("agent_id", sa.String(length=36), nullable=True),
        sa.Column("provider", sa.String(length=16), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("expires_at", UtcDateTime(), nullable=False),
        sa.Column("revoked_at", UtcDateTime(), nullable=True),
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("workspace_id", sa.String(length=36), nullable=False),
        sa.Column("created_at", UtcDateTime(), nullable=False),
        sa.Column("updated_at", UtcDateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["agent_id"],
            ["agent.id"],
            name=op.f("fk_run_token_agent_id_agent"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["run_id"], ["run.id"], name=op.f("fk_run_token_run_id_run"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["run_step_id"],
            ["run_step.id"],
            name=op.f("fk_run_token_run_step_id_run_step"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspace.id"],
            name=op.f("fk_run_token_workspace_id_workspace"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_run_token")),
        sa.UniqueConstraint("token_hash", name=op.f("uq_run_token_token_hash")),
    )
    with op.batch_alter_table("run_token", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_run_token_run_id"), ["run_id"], unique=False)
        batch_op.create_index(
            batch_op.f("ix_run_token_workspace_id"), ["workspace_id"], unique=False
        )


def downgrade() -> None:
    with op.batch_alter_table("run_token", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_run_token_workspace_id"))
        batch_op.drop_index(batch_op.f("ix_run_token_run_id"))

    op.drop_table("run_token")
