"""Small shared helpers for graph nodes.

Nodes are deliberately thin; these helpers are the only duplicated plumbing
(loading the run/ticket, applying a run status, and reading the current step
result). They use only ``ScopedRepo`` for tenant rows.
"""

from __future__ import annotations

from typing import Any, cast

from sqlalchemy.ext.asyncio import AsyncSession

from gaia.db.models import Run, Ticket
from gaia.db.repo import ScopedRepo
from gaia.domain.enums import RunStatus
from gaia.domain.state import transition
from gaia.orchestrator.state import RunState


async def load_run(session: AsyncSession, state: RunState) -> Run:
    """Load the run row for ``state``, scoped to ``workspace_id``."""
    run_id = state["run_id"]
    workspace_id = state["workspace_id"]
    run = await ScopedRepo(session, Run, workspace_id).get(run_id)
    if run is None:
        raise ValueError(f"no run {run_id!r} in workspace {workspace_id!r}")
    return run


async def load_ticket(session: AsyncSession, state: RunState) -> Ticket:
    """Load the ticket row for ``state``, scoped to ``workspace_id``."""
    ticket_id = state["ticket_id"]
    workspace_id = state["workspace_id"]
    ticket = await ScopedRepo(session, Ticket, workspace_id).get(ticket_id)
    if ticket is None:
        raise ValueError(f"no ticket {ticket_id!r} in workspace {workspace_id!r}")
    return ticket


async def set_run_status(session: AsyncSession, state: RunState, status: RunStatus) -> Run:
    """Apply ``status`` to the state's run and flush the change."""
    run = await load_run(session, state)
    transition(run, status)
    await session.flush()
    return run


def current_step_result(state: RunState) -> dict[str, Any] | None:
    """Return the latest stored result for ``state["step_index"]``, if any."""
    step_index = state.get("step_index", 0)
    results = state.get("step_results") or []
    for result in reversed(results):
        if result.get("step_index") == step_index:
            return cast(dict[str, Any], result)
    return None
