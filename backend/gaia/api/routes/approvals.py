"""Read-only approval inbox for the frontend shell (doc 16).

The decision endpoint (``POST /approvals/{id}/decide``) belongs to doc 14 and is
deliberately absent here. This route exists because the shell's nav badge and the
placeholder approvals page need one workspace-scoped list without waiting for 14.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query

from gaia.api.deps import ScopedDep
from gaia.db.models import Approval
from gaia.db.repo import ApprovalRepo
from gaia.domain.enums import ApprovalStatus
from gaia.domain.schemas import ApprovalPage, ApprovalRead

router = APIRouter(tags=["approvals"])


@router.get("/approvals", response_model=ApprovalPage, summary="List approval requests")
async def list_approvals(
    scoped: ScopedDep,
    status: Annotated[ApprovalStatus | None, Query()] = None,
    run_id: Annotated[str | None, Query(max_length=36)] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 100,
) -> ApprovalPage:
    """Return approvals oldest first, optionally filtered by status or run.

    ``total_pending`` always counts the workspace's pending approvals, whatever
    filters the caller passed: the badge makes one call and must not change with
    the page. ``ApprovalRepo`` keeps every read inside the authenticated
    workspace.
    """
    approvals = ApprovalRepo(scoped.session, Approval, scoped.workspace_id)
    rows = await approvals.list_inbox(
        status=status.value if status is not None else None,
        run_id=run_id,
        limit=limit,
    )
    return ApprovalPage(
        items=[ApprovalRead.model_validate(row) for row in rows],
        total_pending=await approvals.count_pending(),
    )


# TODO(gaia-14): add POST /approvals/{id}/decide here.
