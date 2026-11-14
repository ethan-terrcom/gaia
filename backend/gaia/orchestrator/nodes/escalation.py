"""``escalation``: the human gate for a step or CI that needs an owner."""

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


async def escalation(state: RunState, deps: GraphDeps) -> dict[str, Any]:
    """Ask the owner how to proceed, then apply the decision."""
    step_index = state.get("step_index", 0)
    attempt = state.get("attempt", 1)
    key = f"escalation:{step_index}:{attempt}"
    source = state.get("error", "step_escalation")

    async with session_scope(deps.sessions) as session:
        await set_run_status(session, state, RunStatus.AWAITING_APPROVAL)
        approval, created = await upsert_approval(
            session,
            workspace_id=state["workspace_id"],
            run_id=state["run_id"],
            idempotency_key=key,
            kind=ApprovalKind.ESCALATION,
            payload={"kind": ApprovalKind.ESCALATION.value, "source": source},
        )
        approval_id = approval.id

    if created:
        await deps.events.publish(
            workspace_id=state["workspace_id"],
            type=EventType.APPROVAL_REQUESTED,
            payload={"approval_id": approval_id, "kind": ApprovalKind.ESCALATION.value},
            run_id=state["run_id"],
        )

    raw_decision = interrupt(
        {"kind": ApprovalKind.ESCALATION.value, "approval_id": approval_id, "source": source}
    )
    decision = Decision.model_validate(raw_decision)

    async with session_scope(deps.sessions) as session:
        run = await load_run(session, state)
        approvals = ScopedRepo(session, Approval, state["workspace_id"])
        approval_row = (await approvals.list(run_id=state["run_id"], idempotency_key=key, limit=1))[
            0
        ]
        await apply_decision(session, approval_row, decision)

        if decision.action == "reject":
            transition(run, RunStatus.FAILED)
            route: dict[str, Any] = {"route": "end", "error": "escalation_rejected"}
        elif source == "ci_failed":
            route = {"route": "watch_ci", "error": None}
        else:
            route = {
                "route": "implement_step",
                "attempt": attempt + 1,
                "feedback": state.get("feedback"),
                "error": None,
            }

    await deps.events.publish(
        workspace_id=state["workspace_id"],
        type=EventType.APPROVAL_DECIDED,
        payload={"approval_id": approval.id, "action": decision.action},
        run_id=state["run_id"],
    )
    return route
