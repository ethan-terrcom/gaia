"""``lead_final_review``: the lead's review of the complete diff."""

from __future__ import annotations

from typing import Any

from gaia.db.engine import session_scope
from gaia.domain.enums import RunStatus
from gaia.orchestrator.deps import GraphDeps
from gaia.orchestrator.nodes._common import set_run_status
from gaia.orchestrator.state import RunState


async def lead_final_review(state: RunState, deps: GraphDeps) -> dict[str, Any]:
    """Run the final review and store the verdict."""
    async with session_scope(deps.sessions) as session:
        await set_run_status(session, state, RunStatus.REVIEWING)

    review = await deps.reviewer.final_review(state["run_id"])
    return {"review": review.model_dump(), "error": None}
