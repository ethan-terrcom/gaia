"""``open_pr``: open the PR and move the ticket to ``in_review``."""

from __future__ import annotations

from typing import Any

from gaia.db.engine import session_scope
from gaia.domain.enums import RunStatus, TicketStatus
from gaia.domain.state import transition
from gaia.orchestrator.deps import GraphDeps
from gaia.orchestrator.nodes._common import load_run, load_ticket
from gaia.orchestrator.state import RunState


async def open_pr(state: RunState, deps: GraphDeps) -> dict[str, Any]:
    """Open the PR through the interface and record its number."""
    pr_number = await deps.pull_requests.open_pr(state["run_id"])
    async with session_scope(deps.sessions) as session:
        run = await load_run(session, state)
        transition(run, RunStatus.PR_OPEN)
        ticket = await load_ticket(session, state)
        transition(ticket, TicketStatus.IN_REVIEW)
    return {"pr_number": pr_number, "error": None}
