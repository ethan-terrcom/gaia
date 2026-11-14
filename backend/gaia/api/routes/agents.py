"""The agent roster for the dashboard (doc 06).

For now the roster is the two seeded static rows; live status arrives with the
runtime packages later, so the API simply returns the rows the seed created.
"""

from __future__ import annotations

from fastapi import APIRouter

from gaia.api.deps import ScopedDep
from gaia.db.models import Agent
from gaia.domain.schemas import AgentRead

router = APIRouter(tags=["agents"])


@router.get("/agents", response_model=list[AgentRead], summary="List agents")
async def list_agents(scoped: ScopedDep) -> list[AgentRead]:
    """Return the workspace's agents, newest first by UUIDv7 id."""
    rows = await scoped.repo(Agent).list()
    return [AgentRead.model_validate(row) for row in rows]
