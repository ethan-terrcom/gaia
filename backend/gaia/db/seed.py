"""Seed the rows Gaia needs before anything else exists (doc 03 task 7).

A fresh database has no workspace, so nothing can be scoped and no user can own
anything. The first start creates exactly two rows - one workspace and one local
user - through this module, and calling it again is a no-op.

Doc 04 calls :func:`seed_initial_data` from the app lifespan; until then,
``# TODO(gaia-04)`` below is the only caller it needs.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from gaia.db.models import User, Workspace
from gaia.db.repo import ScopedRepo

#: The local workspace's stable handle. ``slug`` is what config and the CLI quote.
DEFAULT_WORKSPACE_SLUG = "local"
DEFAULT_WORKSPACE_NAME = "Local"
DEFAULT_USER_EMAIL = "owner@localhost"
DEFAULT_USER_NAME = "Owner"


@dataclass(frozen=True)
class Seeded:
    """The two rows every other row hangs off."""

    workspace: Workspace
    user: User


async def _default_workspace(session: AsyncSession) -> Workspace:
    """Return the local workspace, creating it the first time.

    ``workspace`` is the one table :class:`~gaia.db.repo.ScopedRepo` cannot read:
    it *is* the tenant, so it has no ``workspace_id`` to filter on.
    """
    workspace = (
        await session.scalars(select(Workspace).where(Workspace.slug == DEFAULT_WORKSPACE_SLUG))
    ).one_or_none()
    if workspace is not None:
        return workspace
    workspace = Workspace(name=DEFAULT_WORKSPACE_NAME, slug=DEFAULT_WORKSPACE_SLUG)
    session.add(workspace)
    await session.flush()
    return workspace


async def _default_user(session: AsyncSession, workspace_id: str) -> User:
    """Return the local owner, creating it the first time."""
    users = ScopedRepo(session, User, workspace_id)
    existing = await users.list(email=DEFAULT_USER_EMAIL)
    if existing:
        return existing[0]
    return await users.create(email=DEFAULT_USER_EMAIL, display_name=DEFAULT_USER_NAME)


async def seed_initial_data(session: AsyncSession) -> Seeded:
    """Create the default workspace and its local user if they are missing.

    Flushes but does not commit: the caller owns the transaction, so a failure
    later in startup cannot leave half a tenant behind.
    """
    workspace = await _default_workspace(session)
    user = await _default_user(session, workspace.id)
    return Seeded(workspace=workspace, user=user)


# TODO(gaia-04): call seed_initial_data() from the app lifespan on first start.
