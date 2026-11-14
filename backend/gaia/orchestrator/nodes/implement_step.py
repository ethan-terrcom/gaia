"""``implement_step``: run one junior implementation attempt (doc 12a)."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from gaia.db.engine import session_scope
from gaia.db.models import RunStep
from gaia.db.repo import ScopedRepo
from gaia.domain.enums import RunStatus, RunStepStatus, TicketStatus
from gaia.domain.state import transition
from gaia.orchestrator.deps import GraphDeps
from gaia.orchestrator.nodes._common import load_run, load_ticket
from gaia.orchestrator.state import RunState


async def implement_step(state: RunState, deps: GraphDeps) -> dict[str, Any]:
    """Run the junior runtime for the current step."""
    run_id = state["run_id"]
    step_index = state["step_index"]
    attempt = state.get("attempt", 1)

    async with session_scope(deps.sessions) as session:
        run = await load_run(session, state)
        transition(run, RunStatus.IMPLEMENTING)

        if step_index == 0:
            ticket = await load_ticket(session, state)
            if ticket.status == TicketStatus.BACKLOG.value:
                transition(ticket, TicketStatus.READY)
            transition(ticket, TicketStatus.IN_PROGRESS)

        step_rows = ScopedRepo(session, RunStep, state["workspace_id"])
        step_row = next(
            iter(await step_rows.list(run_id=run_id, step_index=step_index, limit=1)),
            None,
        )
        if step_row is not None:
            transition(step_row, RunStepStatus.RUNNING)
            step_row.attempts = attempt

    outcome = await deps.steps.implement(
        run_id,
        step_index,
        attempt,
        state.get("feedback"),
    )

    results = list(state.get("step_results") or [])
    results.append(
        {
            "step_index": step_index,
            "attempt": attempt,
            "outcome": asdict(outcome),
            "checks": None,
        }
    )
    return {"feedback": None, "step_results": results}
