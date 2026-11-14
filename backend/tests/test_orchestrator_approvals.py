"""Direct tests for the approval decision helper."""

from __future__ import annotations

import pytest
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from gaia.domain.enums import ApprovalKind, ApprovalStatus
from gaia.orchestrator.approvals import Decision, apply_decision, upsert_approval
from tests.db_builders import make_chain


def test_an_edit_decision_must_include_a_plan() -> None:
    with pytest.raises(ValidationError, match="edit decision must include a plan"):
        Decision(action="edit")


async def test_apply_decision_records_who_decided(session: AsyncSession) -> None:
    """Lead review 12a (A1): the approval row names the deciding user."""
    chain = await make_chain(session, slug="decided-by")
    row, created = await upsert_approval(
        session,
        workspace_id=chain.workspace.id,
        run_id=chain.run.id,
        idempotency_key="approve_plan:0:1",
        kind=ApprovalKind.PLAN,
        payload={"kind": "plan"},
    )
    assert created

    await apply_decision(
        session, row, Decision(action="approve", comment="ok", decided_by=chain.user.id)
    )

    assert row.status == ApprovalStatus.APPROVED.value
    assert row.decided_by == chain.user.id
    assert row.decided_at is not None
