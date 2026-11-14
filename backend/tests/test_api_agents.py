"""Agent roster endpoint (doc 06)."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from gaia.api.deps import Scoped
from gaia.api.routes.agents import list_agents
from gaia.db.models import Agent
from gaia.db.repo import ScopedRepo
from gaia.domain.enums import AgentRole, RuntimeKind
from tests.conftest import Api
from tests.db_builders import make_workspace


async def test_list_agents_returns_the_seeded_roster(api: Api) -> None:
    response = await api.client.get("/api/v1/agents", headers=api.auth)

    assert response.status_code == 200
    body = response.json()
    by_name = {agent["display_name"]: agent for agent in body}

    assert set(by_name) == {"lead", "junior-1"}
    assert by_name["lead"]["role"] == "lead"
    assert by_name["lead"]["runtime"] == "native_claude"
    assert by_name["junior-1"]["role"] == "junior"
    assert by_name["junior-1"]["runtime"] == "native_deepseek"


async def test_list_agents_function_directly(session: AsyncSession) -> None:
    """Call the route function directly so coverage sees the return expression."""
    workspace = await make_workspace(session, slug="agents-direct")
    agents = ScopedRepo(session, Agent, workspace.id)
    await agents.create(
        role=AgentRole.LEAD,
        runtime=RuntimeKind.NATIVE_CLAUDE,
        model="claude-opus-5-5",
        display_name="lead-direct",
    )

    result = await list_agents(Scoped(session, workspace.id))

    assert [agent.display_name for agent in result] == ["lead-direct"]
