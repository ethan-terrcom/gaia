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
from decimal import Decimal
from functools import wraps
from typing import Any

from langgraph.errors import GraphBubbleUp
from langgraph.types import interrupt
from sqlalchemy.ext.asyncio import AsyncSession

from gaia.db.engine import session_scope
from gaia.db.models import Approval, Run
from gaia.db.repo import ScopedRepo
from gaia.domain.enums import ApprovalKind, EventType, RunStatus
from gaia.domain.state import transition
from gaia.orchestrator.approvals import Decision, apply_decision, upsert_approval
from gaia.orchestrator.deps import GraphDeps
from gaia.orchestrator.state import RunState
from gaia.security.redaction import redact
from gaia.services.costs import remaining_budget

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
    await deps.events.publish(
        workspace_id=workspace_id,
        type=EventType.RUN_FAILED,
        payload={"node": node_name, "error": error},
        run_id=run_id,
    )
    return error


def with_guard(
    deps: GraphDeps, node_name: str
) -> Callable[
    [Callable[[RunState], Awaitable[dict[str, Any] | None]]],
    Callable[[RunState], Awaitable[dict[str, Any]]],
]:
    """Decorate a node function with the shared guard envelope."""

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
                    return {}

            # 2. Budget check. The approval row is upserted before interrupting,
            # because this whole wrapper re-runs from its first line on resume.
            budget_key: str | None = None
            budget_payload: dict[str, Any] | None = None
            budget_created = False
            async with session_scope(deps.sessions) as session:
                run = await _load_run(session, state)
                if remaining_budget(run) <= 0:
                    transition(run, RunStatus.PAUSED_BUDGET)
                    budget_key = f"budget:{node_name}:{run.cost_usd}"
                    approval, budget_created = await upsert_approval(
                        session,
                        workspace_id=workspace_id,
                        run_id=run_id,
                        idempotency_key=budget_key,
                        kind=ApprovalKind.BUDGET,
                        payload={
                            "kind": ApprovalKind.BUDGET.value,
                            "node": node_name,
                            "run_id": run_id,
                            "cost_usd": str(run.cost_usd),
                            "budget_usd": str(run.budget_usd),
                        },
                    )
                    budget_payload = {
                        "kind": ApprovalKind.BUDGET.value,
                        "approval_id": approval.id,
                        "node": node_name,
                        "run_id": run_id,
                        "cost_usd": str(run.cost_usd),
                        "budget_usd": str(run.budget_usd),
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
                    return {}

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
