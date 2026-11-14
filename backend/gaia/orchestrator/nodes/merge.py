"""``merge``: merge the approved PR and move the ticket to ``done``."""

from __future__ import annotations

from typing import Any

from gaia.db.engine import session_scope
from gaia.domain.enums import RunStatus, TicketStatus
from gaia.domain.state import transition
from gaia.orchestrator.deps import GraphDeps
from gaia.orchestrator.nodes._common import load_run, load_ticket
from gaia.orchestrator.state import RunState


async def merge(state: RunState, deps: GraphDeps) -> dict[str, Any]:
    """Merge the PR and mark the ticket done."""
    pr_number = state.get("pr_number")
    if pr_number is None:
        raise ValueError("merge requires pr_number")
    await deps.pull_requests.merge(state["run_id"], pr_number)

    async with session_scope(deps.sessions) as session:
        run = await load_run(session, state)
        transition(run, RunStatus.PR_OPEN)
        ticket = await load_ticket(session, state)
        transition(ticket, TicketStatus.DONE)
    return {"error": None}
