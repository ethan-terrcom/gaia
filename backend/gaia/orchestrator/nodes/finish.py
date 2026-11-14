"""``finish``: mark the completed run terminal after the merge."""

from __future__ import annotations

from typing import Any

from gaia.db.engine import session_scope
from gaia.domain.enums import RunStatus
from gaia.orchestrator.deps import GraphDeps
from gaia.orchestrator.nodes._common import set_run_status
from gaia.orchestrator.state import RunState


async def finish(state: RunState, deps: GraphDeps) -> dict[str, Any]:
    """Set the run to ``completed``."""
    async with session_scope(deps.sessions) as session:
        await set_run_status(session, state, RunStatus.COMPLETED)
    return {"error": None}
