"""``run_checks``: run the step's ``test_command`` (doc 12a)."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from gaia.db.engine import session_scope
from gaia.domain.enums import RunStatus
from gaia.domain.state import transition
from gaia.orchestrator.deps import GraphDeps
from gaia.orchestrator.nodes._common import load_run
from gaia.orchestrator.state import RunState


async def run_checks(state: RunState, deps: GraphDeps) -> dict[str, Any]:
    """Run checks for the current step and attach the result to its latest outcome."""
    async with session_scope(deps.sessions) as session:
        run = await load_run(session, state)
        transition(run, RunStatus.IMPLEMENTING)

    checks = await deps.checks.run(state["run_id"], state["step_index"])
    results = list(state.get("step_results") or [])
    if not results:
        raise ValueError("run_checks requires an implementation outcome")
    results[-1] = {**results[-1], "checks": asdict(checks)}
    return {"step_results": results}
