"""First-start seeding (doc 03 task 7).

A fresh database needs one workspace and one local user before any other row can
exist. Seeding runs on every start, so the second call must be a no-op.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from gaia.db.models import User, Workspace
from gaia.db.seed import (
    DEFAULT_USER_EMAIL,
    DEFAULT_WORKSPACE_SLUG,
    seed_initial_data,
)


async def test_a_fresh_database_gets_one_workspace_and_one_user(
    session: AsyncSession,
) -> None:
    """The defaults are the ones config and the CLI quote."""
    seeded = await seed_initial_data(session)

    assert seeded.workspace.slug == DEFAULT_WORKSPACE_SLUG
    assert seeded.user.email == DEFAULT_USER_EMAIL
    assert seeded.user.workspace_id == seeded.workspace.id


async def test_seeding_twice_changes_nothing(session: AsyncSession) -> None:
    """The second start returns the same rows instead of adding more."""
    first = await seed_initial_data(session)
    second = await seed_initial_data(session)

    assert second.workspace.id == first.workspace.id
    assert second.user.id == first.user.id
    assert len((await session.scalars(select(Workspace))).all()) == 1
    assert len((await session.scalars(select(User))).all()) == 1
