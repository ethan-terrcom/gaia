"""``git_commit_step``: commit an accepted step and advance the plan."""

from __future__ import annotations

from typing import Any

from gaia.db.engine import session_scope
from gaia.db.models import RunStep
from gaia.db.repo import ScopedRepo
from gaia.domain.enums import RunStatus, RunStepStatus
from gaia.domain.state import transition
from gaia.orchestrator.deps import GraphDeps
from gaia.orchestrator.nodes._common import load_run
from gaia.orchestrator.state import RunState


async def git_commit_step(state: RunState, deps: GraphDeps) -> dict[str, Any]:
    """Commit the accepted step, then go to the next step or final review."""
    plan = state.get("plan")
    if plan is None:
        raise ValueError("git_commit_step requires a plan")
    steps = plan.get("steps")
    if not isinstance(steps, list):
        raise ValueError("plan.steps must be a list")
    step_index = state["step_index"]
    if step_index >= len(steps):
        raise ValueError("step_index is outside the plan")
    title = steps[step_index].get("title", f"Step {step_index + 1}")

    sha = await deps.committer.commit_step(state["run_id"], step_index, title)

    async with session_scope(deps.sessions) as session:
        run = await load_run(session, state)
        transition(run, RunStatus.IMPLEMENTING)
        step_rows = ScopedRepo(session, RunStep, state["workspace_id"])
        step_row = next(
            iter(await step_rows.list(run_id=state["run_id"], step_index=step_index, limit=1)),
            None,
        )
        if step_row is not None:
            transition(step_row, RunStepStatus.COMPLETED)
            step_row.result_summary = f"committed as {sha}"

    if step_index + 1 < len(steps):
        return {
            "step_index": step_index + 1,
            "attempt": 1,
            "feedback": None,
            "review": None,
            "route": "implement_step",
            "error": None,
        }
    return {"route": "lead_final_review", "error": None}
