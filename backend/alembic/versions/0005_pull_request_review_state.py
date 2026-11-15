"""pull request review state and head sha

Doc 15b stores GitHub's review rollup and the PR head SHA that GitHub reported,
so the PR sync service can tell when a human changed a PR outside Gaia.

Revision ID: 0005
Revises: 0004
Create Date: 2026-11-14 10:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("pull_request", schema=None) as batch_op:
        batch_op.add_column(sa.Column("review_state", sa.String(length=32), nullable=True))
        batch_op.add_column(sa.Column("head_sha", sa.String(length=64), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("pull_request", schema=None) as batch_op:
        batch_op.drop_column("head_sha")
        batch_op.drop_column("review_state")
