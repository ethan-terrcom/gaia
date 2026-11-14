"""First-start seeding (doc 03 task 7 and doc 06 task 3).

A fresh database needs one workspace, one local user and the two agent rows
before any other row can exist. Seeding runs on every start, so the second call
must be a no-op.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from gaia.config.settings import Settings
from gaia.db.models import Agent, User, Workspace
from gaia.db.seed import (
    DEFAULT_USER_EMAIL,
    DEFAULT_WORKSPACE_SLUG,
    seed_initial_data,
)
from gaia.domain.enums import AgentRole, RuntimeKind


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    """Settings with the default lead/junior models; the seed only reads them."""
    return Settings(data_dir=tmp_path / "gaia-data")


async def test_a_fresh_database_gets_one_workspace_and_one_user(
    session: AsyncSession,
    settings: Settings,
) -> None:
    """The defaults are the ones config and the CLI quote."""
    seeded = await seed_initial_data(session, settings)

    assert seeded.workspace.slug == DEFAULT_WORKSPACE_SLUG
    assert seeded.user.email == DEFAULT_USER_EMAIL
    assert seeded.user.workspace_id == seeded.workspace.id


async def test_a_fresh_database_gets_the_two_seeded_agents(
    session: AsyncSession,
    settings: Settings,
) -> None:
    """Doc 06 task 3: ``lead`` drives Claude, ``junior-1`` drives DeepSeek."""
    await seed_initial_data(session, settings)

    agents = list((await session.scalars(select(Agent))).all())
    by_name = {agent.display_name: agent for agent in agents}

    assert set(by_name) == {"lead", "junior-1"}
    assert by_name["lead"].role == AgentRole.LEAD
    assert by_name["lead"].runtime == RuntimeKind.NATIVE_CLAUDE
    assert by_name["lead"].model == settings.lead_model
    assert by_name["junior-1"].role == AgentRole.JUNIOR
    assert by_name["junior-1"].runtime == RuntimeKind.NATIVE_DEEPSEEK
    assert by_name["junior-1"].model == settings.junior_model


async def test_seeding_twice_changes_nothing(
    session: AsyncSession,
    settings: Settings,
) -> None:
    """The second start returns the same rows instead of adding more."""
    first = await seed_initial_data(session, settings)
    second = await seed_initial_data(session, settings)

    assert second.workspace.id == first.workspace.id
    assert second.user.id == first.user.id
    assert len((await session.scalars(select(Workspace))).all()) == 1
    assert len((await session.scalars(select(User))).all()) == 1
    assert len((await session.scalars(select(Agent))).all()) == 2
