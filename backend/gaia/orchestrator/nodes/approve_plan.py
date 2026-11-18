"""``approve_plan``: the human gate on the lead's plan."""

from __future__ import annotations

from typing import Any

from langgraph.types import interrupt
from sqlalchemy.ext.asyncio import AsyncSession

from gaia.db.engine import session_scope
from gaia.db.models import Approval, RunStep
from gaia.db.repo import ScopedRepo
from gaia.domain.enums import ApprovalKind, EventType, RunStatus, RunStepStatus
from gaia.domain.plans import Plan
from gaia.domain.state import transition
from gaia.orchestrator.approvals import Decision, apply_decision, upsert_approval
from gaia.orchestrator.deps import GraphDeps
from gaia.orchestrator.nodes._common import load_run, set_run_status
from gaia.orchestrator.state import RunState


async def _replace_pending_steps(session: AsyncSession, state: RunState, plan: Plan) -> None:
    """Delete pending steps and insert the replacement plan's steps."""
    steps = ScopedRepo(session, RunStep, state["workspace_id"])
    existing = await steps.list(run_id=state["run_id"])
    for existing_step in existing:
        if existing_step.status == RunStepStatus.PENDING.value:
            await steps.delete(existing_step.id)
    for step_index, plan_step in enumerate(plan.steps):
        await steps.create(
            run_id=state["run_id"],
            step_index=step_index,
            title=plan_step.title,
            spec_json=plan_step.model_dump(),
            status=RunStepStatus.PENDING.value,
        )


def _gate_payload(state: RunState) -> dict[str, Any]:
    """The plan approval's payload: the plan, plus the crowd's extras when present."""
    payload: dict[str, Any] = {"kind": ApprovalKind.PLAN.value, "plan": state.get("plan")}
    meta = state.get("plan_meta") or {}
    for key in ("candidates", "provenance", "notes"):
        if meta.get(key):
            payload[key] = meta[key]
    return payload


async def approve_plan(state: RunState, deps: GraphDeps) -> dict[str, Any]:
    """Ask the owner to approve the plan, then apply the decision.

    Straight shot (doc 19b, M1) has no plan to approve: starting the run was the
    go-ahead, so the gate routes straight on and writes no approval row.
    """
    if (state.get("plan_meta") or {}).get("skip_gate"):
        return {"route": "implement_step"}
    step_index = state.get("step_index", 0)
    attempt = state.get("attempt", 1)
    key = f"approve_plan:{step_index}:{attempt}"

    async with session_scope(deps.sessions) as session:
        await set_run_status(session, state, RunStatus.AWAITING_APPROVAL)
        approval, created = await upsert_approval(
            session,
            workspace_id=state["workspace_id"],
            run_id=state["run_id"],
            idempotency_key=key,
            kind=ApprovalKind.PLAN,
            payload=_gate_payload(state),
        )
        approval_id = approval.id

    if created:
        await deps.events.publish(
            workspace_id=state["workspace_id"],
            type=EventType.APPROVAL_REQUESTED,
            payload={"approval_id": approval_id, "kind": ApprovalKind.PLAN.value},
            run_id=state["run_id"],
        )

    raw_decision = interrupt({**_gate_payload(state), "approval_id": approval_id})
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
            await session.flush()
            route: dict[str, Any] = {"route": "end", "error": "plan_rejected"}
        else:
            if decision.action == "edit":
                plan = Plan.model_validate(decision.plan)
                await _replace_pending_steps(session, state, plan)
                route = {"plan": plan.model_dump(), "route": "implement_step"}
            else:
                route = {"route": "implement_step"}

    await deps.events.publish(
        workspace_id=state["workspace_id"],
        type=EventType.APPROVAL_DECIDED,
        payload={"approval_id": approval.id, "action": decision.action},
        run_id=state["run_id"],
    )
    return route
