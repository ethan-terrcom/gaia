"""``lead_plan``: call the planner and persist ``run_step`` rows idempotently."""

from __future__ import annotations

from typing import Any

from gaia.db.engine import session_scope
from gaia.db.models import RunStep
from gaia.db.repo import ScopedRepo
from gaia.domain.enums import RunStatus
from gaia.domain.state import transition
from gaia.orchestrator.deps import GraphDeps
from gaia.orchestrator.nodes._common import load_run
from gaia.orchestrator.state import RunState


async def lead_plan(state: RunState, deps: GraphDeps) -> dict[str, Any]:
    """Produce the plan and persist its steps exactly once."""
    outcome = await deps.planner.plan(state["run_id"])
    plan = outcome.plan
    async with session_scope(deps.sessions) as session:
        run = await load_run(session, state)
        transition(run, RunStatus.PLANNING)
        steps = ScopedRepo(session, RunStep, state["workspace_id"])
        existing = await steps.list(run_id=state["run_id"])
        if not existing:
            for step_index, step in enumerate(plan.steps):
                await steps.create(
                    run_id=state["run_id"],
                    step_index=step_index,
                    title=step.title,
                    spec_json=step.model_dump(),
                )
    return {"plan": plan.model_dump(), "plan_meta": outcome.meta()}
