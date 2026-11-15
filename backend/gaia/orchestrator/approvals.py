"""Approval-row helpers shared by every interrupting node and the budget guard.

An interrupting node re-runs from its first line on resume. The row must
therefore be an *upsert* keyed by ``(run_id, idempotency_key)``; a select-first
is the implementation here. The decision is validated with a Pydantic model
before it is applied.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, model_validator
from sqlalchemy.ext.asyncio import AsyncSession

from gaia.db.models import Approval
from gaia.db.repo import ScopedRepo
from gaia.db.types import utcnow
from gaia.domain.enums import ApprovalKind, ApprovalStatus


class Decision(BaseModel):
    """The shape of every graph-gate resume value.

    ``budget_usd`` is extra data for the single budget interrupt; the other
    fields are shared by the regular gates.
    """

    action: Literal["approve", "reject", "edit"]
    comment: str | None = None
    plan: dict[str, Any] | None = None
    budget_usd: Decimal | None = None
    #: The user who decided. The API (doc 14) fills it from the authenticated
    #: principal; recorded as ``approval.decided_by`` (lead review 12a, A1).
    decided_by: str | None = None

    @model_validator(mode="after")
    def _edit_has_plan(self) -> Decision:
        """An edit decision must carry the replacement plan."""
        if self.action == "edit" and self.plan is None:
            raise ValueError("an edit decision must include a plan")
        return self


def approval_status_for(action: str) -> str:
    """Map a decision action to the stored approval status."""
    if action == "reject":
        return ApprovalStatus.REJECTED.value
    # Both "approve" and "edit" mean the gate was approved; an edit is an
    # approval with a replacement plan.
    return ApprovalStatus.APPROVED.value


async def upsert_approval(
    session: AsyncSession,
    *,
    workspace_id: str,
    run_id: str,
    idempotency_key: str,
    kind: ApprovalKind,
    payload: dict[str, Any],
) -> tuple[Approval, bool]:
    """Return the approval row for ``(run_id, idempotency_key)`` and whether it is new.

    The row is created only when no row exists. This is the idempotent step that
    runs before every graph interrupt; the unique constraint backs it up.
    """
    approvals = ScopedRepo(session, Approval, workspace_id)
    existing = await approvals.list(run_id=run_id, idempotency_key=idempotency_key, limit=1)
    if existing:
        return existing[0], False
    row = await approvals.create(
        run_id=run_id,
        idempotency_key=idempotency_key,
        kind=kind.value,
        payload_json=payload,
        status=ApprovalStatus.PENDING.value,
    )
    return row, True


async def apply_decision(session: AsyncSession, approval: Approval, decision: Decision) -> None:
    """Write the human's decision onto an approval row in ``session``."""
    approval.status = approval_status_for(decision.action)
    approval.comment = decision.comment
    approval.decided_by = decision.decided_by
    approval.decided_at = utcnow()
    await session.flush()


async def expire_pending_approvals(
    session: AsyncSession, *, workspace_id: str, run_id: str, reason: str
) -> list[str]:
    """Mark every still-pending approval of a run ``expired`` and return their ids.

    A cancelled or failed run will never act on them. Left pending, they sit in
    the inbox, and deciding one would resume a graph that has ended (found in
    the first live end-to-end run).
    """
    approvals = ScopedRepo(session, Approval, workspace_id)
    expired: list[str] = []
    for row in await approvals.list(run_id=run_id, status=ApprovalStatus.PENDING.value):
        row.status = ApprovalStatus.EXPIRED.value
        row.decided_at = utcnow()
        row.comment = reason
        expired.append(row.id)
    return expired
