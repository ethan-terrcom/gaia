"""The ``with_guard`` node wrapper (doc 12a).

Every graph node is wrapped in the same envelope:

1. load the run row and return ``{}`` if it is already terminal;
2. if the run is out of budget, pause it behind the budget interrupt;
3. emit ``run.node_started``, run the node, emit ``run.node_finished``;
4. let LangGraph control-flow exceptions bubble up untouched; for any other
   exception mark the run ``failed``, emit ``run.failed`` and raise
   :class:`RunFailed` so the graph stops.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import datetime
from decimal import Decimal
from functools import wraps
from typing import Any

import structlog
from langgraph.errors import GraphBubbleUp
from langgraph.types import interrupt
from sqlalchemy.ext.asyncio import AsyncSession

from gaia.db.engine import session_scope
from gaia.db.models import Approval, Run
from gaia.db.repo import ScopedRepo, count_decided_budget_approvals
from gaia.db.types import utcnow
from gaia.domain.enums import ApprovalKind, ApprovalStatus, EventType, RunStatus
from gaia.domain.state import transition
from gaia.orchestrator.approvals import (
    Decision,
    apply_decision,
    expire_pending_approvals,
    upsert_approval,
)
from gaia.orchestrator.deps import GraphDeps
from gaia.orchestrator.state import RunState
from gaia.security.redaction import redact
from gaia.services.costs import daily_spend, remaining_budget

logger = structlog.get_logger(__name__)

TERMINAL_RUN_STATUSES = frozenset(
    {RunStatus.FAILED.value, RunStatus.COMPLETED.value, RunStatus.CANCELLED.value}
)


class RunFailed(Exception):
    """Raised after a node failure has been persisted, to stop the graph."""

    def __init__(self, run_id: str, node_name: str, error: str) -> None:
        self.run_id = run_id
        self.node_name = node_name
        self.error = error
        super().__init__(f"run {run_id!r} failed in node {node_name!r}: {error}")


async def _load_run(session: AsyncSession, state: RunState) -> Run:
    """Return the state's run row, scoped to the state's workspace."""
    run_id = state.get("run_id")
    workspace_id = state.get("workspace_id")
    if run_id is None or workspace_id is None:
        raise ValueError("with_guard requires run_id and workspace_id in state")
    run = await ScopedRepo(session, Run, workspace_id).get(run_id)
    if run is None:
        raise ValueError(f"no run {run_id!r} in workspace {workspace_id!r}")
    return run


async def _mark_failed(deps: GraphDeps, node_name: str, state: RunState, exc: Exception) -> str:
    """Persist the failed run status and publish ``run.failed``."""
    run_id = state["run_id"]
    workspace_id = state["workspace_id"]
    error = f"{type(exc).__name__}: {redact(str(exc))}"
    async with session_scope(deps.sessions) as session:
        run = await _load_run(session, state)
        transition(run, RunStatus.FAILED)
        expired = await expire_pending_approvals(
            session, workspace_id=workspace_id, run_id=run_id, reason="run failed"
        )
    await deps.events.publish(
        workspace_id=workspace_id,
        type=EventType.RUN_FAILED,
        payload={"node": node_name, "error": error},
        run_id=run_id,
    )
    for approval_id in expired:
        await deps.events.publish(
            workspace_id=workspace_id,
            type=EventType.APPROVAL_DECIDED,
            payload={"approval_id": approval_id, "action": "expired"},
            run_id=run_id,
        )
    return error


async def _pending_budget_approval(
    session: AsyncSession, state: RunState, node_name: str
) -> Approval | None:
    """Return this node's still-pending budget approval for the run, if any."""
    approvals = ScopedRepo(session, Approval, state["workspace_id"])
    rows = await approvals.list(
        run_id=state["run_id"],
        kind=ApprovalKind.BUDGET.value,
        status=ApprovalStatus.PENDING.value,
    )
    prefix = f"budget:{node_name}:"
    matching = [row for row in rows if row.idempotency_key.startswith(prefix)]
    return matching[0] if matching else None


def with_guard(
    deps: GraphDeps,
    node_name: str,
    *,
    now: Callable[[], datetime] = utcnow,
) -> Callable[
    [Callable[[RunState], Awaitable[dict[str, Any] | None]]],
    Callable[[RunState], Awaitable[dict[str, Any]]],
]:
    """Decorate a node function with the shared guard envelope.

    ``now`` is the guard's clock. A graph dependency may carry its own clock in
    ``deps.now`` (for example an 18a test with a frozen UTC day); when it does
    not, ``now`` is used.
    """

    def decorator(
        node: Callable[[RunState], Awaitable[dict[str, Any] | None]],
    ) -> Callable[[RunState], Awaitable[dict[str, Any]]]:
        @wraps(node)
        async def wrapped(state: RunState) -> dict[str, Any]:
            run_id = state["run_id"]
            workspace_id = state["workspace_id"]

            # 1. Load the run and do nothing if it already reached a terminal.
            async with session_scope(deps.sessions) as session:
                run = await _load_run(session, state)
                if run.status in TERMINAL_RUN_STATUSES:
                    # Route to the end explicitly: returning nothing would leave the
                    # previous node's ``route`` in state, and a conditional edge whose
                    # path map lacks it raises (live E2E: KeyError 'escalation').
                    return {"route": "end"}

            # 2. Budget check. The approval row is upserted before interrupting,
            # because this whole wrapper re-runs from its first line on resume.
            # The run cap is checked first, exactly as ``ensure_budget`` does.
            clock = deps.now if deps.now is not None else now
            budget_key: str | None = None
            budget_payload: dict[str, Any] | None = None
            budget_created = False
            budget_scope: str | None = None
            async with session_scope(deps.sessions) as session:
                run = await _load_run(session, state)
                spent_today = Decimal(0)
                daily_limit = Decimal(str(deps.settings.daily_budget_usd))
                # A pending budget approval for this node means this is the
                # re-run on resume: answer *that* approval. Recomputing would
                # miss it whenever the cap no longer applies (the owner raised
                # it, or a new UTC day began), leaving the row pending forever
                # (lead review 18a, B1).
                pending = await _pending_budget_approval(session, state, node_name)
                if pending is not None:
                    budget_scope = str(pending.payload_json.get("scope", "run"))
                    budget_key = pending.idempotency_key
                    budget_payload = {**pending.payload_json, "approval_id": pending.id}
                elif remaining_budget(run) <= 0:
                    budget_scope = "run"
                else:
                    spent_today = await daily_spend(session, workspace_id, now=clock())
                    if spent_today >= daily_limit:
                        budget_scope = "daily"

                if budget_scope is not None and pending is None:
                    transition(run, RunStatus.PAUSED_BUDGET)
                    if budget_scope == "run":
                        # Run scope keeps today's key: the cost is unchanged
                        # while this pause is pending, so the re-run on resume
                        # finds the same still-pending row instead of inserting
                        # a duplicate.
                        budget_key = f"budget:{node_name}:{run.cost_usd}"
                        payload = {
                            "kind": ApprovalKind.BUDGET.value,
                            "node": node_name,
                            "run_id": run_id,
                            "scope": "run",
                            "cost_usd": str(run.cost_usd),
                            "budget_usd": str(run.budget_usd),
                        }
                    else:
                        # Daily scope uses a key unique per pause but stable
                        # across the resume re-run. ``n`` counts this node's
                        # *decided* daily budget approvals, so the current
                        # still-pending row is not counted and the re-run gets
                        # the same key. The next pause, after this row has been
                        # decided, gets ``n + 1`` and therefore a fresh row.
                        count = await count_decided_budget_approvals(
                            session,
                            run_id=run_id,
                            workspace_id=workspace_id,
                            key_prefix=f"budget:{node_name}:daily:",
                        )
                        budget_key = f"budget:{node_name}:daily:{count}"
                        payload = {
                            "kind": ApprovalKind.BUDGET.value,
                            "node": node_name,
                            "run_id": run_id,
                            "scope": "daily",
                            "cost_usd": str(run.cost_usd),
                            "budget_usd": str(run.budget_usd),
                            "spent_today_usd": str(spent_today),
                            "daily_budget_usd": str(daily_limit),
                        }
                    approval, budget_created = await upsert_approval(
                        session,
                        workspace_id=workspace_id,
                        run_id=run_id,
                        idempotency_key=budget_key,
                        kind=ApprovalKind.BUDGET,
                        payload=payload,
                    )
                    budget_payload = {
                        **payload,
                        "approval_id": approval.id,
                    }

            if budget_created and budget_payload is not None:
                await deps.events.publish(
                    workspace_id=workspace_id,
                    type=EventType.APPROVAL_REQUESTED,
                    payload={
                        "approval_id": budget_payload["approval_id"],
                        "kind": budget_payload["kind"],
                    },
                    run_id=run_id,
                )

            if budget_payload is not None and budget_key is not None:
                raw_decision = interrupt(budget_payload)
                decision = Decision.model_validate(raw_decision)
                async with session_scope(deps.sessions) as session:
                    run = await _load_run(session, state)
                    approvals = ScopedRepo(session, Approval, workspace_id)
                    approval_row = (
                        await approvals.list(run_id=run_id, idempotency_key=budget_key, limit=1)
                    )[0]
                    await apply_decision(session, approval_row, decision)
                    if decision.action == "reject":
                        transition(run, RunStatus.CANCELLED)
                    elif budget_scope == "daily":
                        # Approving a daily-scope pause changes no budget: the
                        # daily cap is a hard limit. If the owner raised
                        # ``daily_budget_usd`` (or a new UTC day has started),
                        # the next guard simply passes; if the day is still over
                        # the cap, the next guard pauses again with a new key.
                        if decision.budget_usd is not None:
                            logger.info(
                                "guard.daily_budget_ignored",
                                run_id=run_id,
                                node=node_name,
                            )
                    elif decision.budget_usd is not None:
                        if decision.budget_usd <= run.cost_usd:
                            raise ValueError("budget_usd must be above what the run has spent")
                        run.budget_usd = decision.budget_usd
                    else:
                        # "Approve" without a number grants one more default run
                        # budget. Continuing at the old cap would only make the
                        # gateway refuse the next call (lead review 12a, G1).
                        run.budget_usd = run.cost_usd + Decimal(str(deps.settings.run_budget_usd))
                    decided_approval_id = approval_row.id

                await deps.events.publish(
                    workspace_id=workspace_id,
                    type=EventType.APPROVAL_DECIDED,
                    payload={"approval_id": decided_approval_id, "action": decision.action},
                    run_id=run_id,
                )
                if decision.action == "reject":
                    # The run is cancelled now; end the graph (same reason as step 1).
                    return {"route": "end"}

            # 3. Run the node inside the event envelope.
            await deps.events.publish(
                workspace_id=workspace_id,
                type=EventType.RUN_NODE_STARTED,
                payload={"node": node_name},
                run_id=run_id,
            )
            try:
                update = await node(state)
            except GraphBubbleUp:
                # Interrupts and parent commands are control flow, not failures.
                raise
            except Exception as exc:
                error = await _mark_failed(deps, node_name, state, exc)
                state["error"] = error
                raise RunFailed(run_id, node_name, error) from exc
            await deps.events.publish(
                workspace_id=workspace_id,
                type=EventType.RUN_NODE_FINISHED,
                payload={"node": node_name},
                run_id=run_id,
            )
            return update or {}

        return wrapped

    return decorator
