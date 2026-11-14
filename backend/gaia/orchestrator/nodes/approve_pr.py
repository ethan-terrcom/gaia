"""``approve_pr``: the human gate before opening a PR."""

from __future__ import annotations

from typing import Any

from langgraph.types import interrupt

from gaia.db.engine import session_scope
from gaia.db.models import Approval
from gaia.db.repo import ScopedRepo
from gaia.domain.enums import ApprovalKind, EventType, RunStatus
from gaia.domain.state import transition
from gaia.orchestrator.approvals import Decision, apply_decision, upsert_approval
from gaia.orchestrator.deps import GraphDeps
from gaia.orchestrator.nodes._common import load_run, set_run_status
from gaia.orchestrator.state import RunState


async def approve_pr(state: RunState, deps: GraphDeps) -> dict[str, Any]:
    """Ask the owner to approve opening a PR, then apply the decision."""
    step_index = state.get("step_index", 0)
    attempt = state.get("attempt", 1)
    key = f"approve_pr:{step_index}:{attempt}"

    async with session_scope(deps.sessions) as session:
        await set_run_status(session, state, RunStatus.AWAITING_APPROVAL)
        approval, created = await upsert_approval(
            session,
            workspace_id=state["workspace_id"],
            run_id=state["run_id"],
            idempotency_key=key,
            kind=ApprovalKind.PR_OPEN,
            payload={"kind": ApprovalKind.PR_OPEN.value},
        )
        approval_id = approval.id

    if created:
        await deps.events.publish(
            workspace_id=state["workspace_id"],
            type=EventType.APPROVAL_REQUESTED,
            payload={"approval_id": approval_id, "kind": ApprovalKind.PR_OPEN.value},
            run_id=state["run_id"],
        )

    raw_decision = interrupt({"kind": ApprovalKind.PR_OPEN.value, "approval_id": approval_id})
    decision = Decision.model_validate(raw_decision)

    async with session_scope(deps.sessions) as session:
        run = await load_run(session, state)
        approvals = ScopedRepo(session, Approval, state["workspace_id"])
        approval_row = (await approvals.list(run_id=state["run_id"], idempotency_key=key, limit=1))[
            0
        ]
        await apply_decision(session, approval_row, decision)
        if decision.action == "reject":
            transition(run, RunStatus.CANCELLED)
            route: dict[str, Any] = {"route": "end", "error": "pr_open_rejected"}
        else:
            route = {"route": "open_pr", "error": None}

    await deps.events.publish(
        workspace_id=state["workspace_id"],
        type=EventType.APPROVAL_DECIDED,
        payload={"approval_id": approval.id, "action": decision.action},
        run_id=state["run_id"],
    )
    return route
