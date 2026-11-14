"""``watch_ci``: watch the PR's CI and route a failure to escalation."""

from __future__ import annotations

from typing import Any

from gaia.db.engine import session_scope
from gaia.domain.enums import RunStatus
from gaia.domain.state import transition
from gaia.orchestrator.deps import GraphDeps
from gaia.orchestrator.nodes._common import load_run
from gaia.orchestrator.state import RunState


async def watch_ci(state: RunState, deps: GraphDeps) -> dict[str, Any]:
    """Check CI and choose the next gate."""
    pr_number = state.get("pr_number")
    if pr_number is None:
        raise ValueError("watch_ci requires pr_number")
    result = await deps.pull_requests.watch_ci(state["run_id"], pr_number)

    async with session_scope(deps.sessions) as session:
        run = await load_run(session, state)
        transition(run, RunStatus.PR_OPEN)

    if result == "failed":
        return {"route": "escalation", "error": "ci_failed"}
    return {"route": "approve_merge", "error": None}
