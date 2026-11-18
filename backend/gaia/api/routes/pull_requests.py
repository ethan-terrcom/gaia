"""List and detail for the pull requests Gaia opened (doc 15b + PR desk)."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, status

from gaia.api.deps import ScopedDep
from gaia.db.models import Approval, Worktree
from gaia.db.repo import ApprovalRepo, PullRequestRepo, PullRequestWithContext, ScopedRepo
from gaia.domain.enums import ApprovalKind, ApprovalStatus, PullRequestState
from gaia.domain.schemas import (
    PullRequestDetail,
    PullRequestPage,
    PullRequestPendingApproval,
    PullRequestRead,
)

router = APIRouter(tags=["pull-requests"])

_PR_DESK_KINDS = frozenset({ApprovalKind.PR_OPEN.value, ApprovalKind.MERGE.value})


def _read_from_row(row: PullRequestWithContext) -> dict[str, object]:
    """Shared fields for list and detail responses."""
    return {
        "id": row.pull_request.id,
        "number": row.pull_request.number,
        "url": row.pull_request.url,
        "state": row.pull_request.state,
        "ci_status": row.pull_request.ci_status,
        "review_state": row.pull_request.review_state,
        "head_sha": row.pull_request.head_sha,
        "run_id": row.pull_request.run_id,
        "run_status": row.run.status,
        "ticket_id": row.ticket.id,
        "ticket_title": row.ticket.title,
        "repo_name": row.repository.name,
        "created_at": row.pull_request.created_at,
        "updated_at": row.pull_request.updated_at,
    }


@router.get("/pull-requests", response_model=PullRequestPage, summary="List pull requests")
async def list_pull_requests(
    scoped: ScopedDep,
    state_filter: Annotated[PullRequestState | None, Query(alias="state")] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 100,
) -> PullRequestPage:
    """Return the workspace's PRs, newest first, optionally filtered by state."""
    rows = await PullRequestRepo(scoped.session).list_with_context(
        workspace_id=scoped.workspace_id,
        state=state_filter.value if state_filter is not None else None,
        limit=limit,
    )
    items = [PullRequestRead.model_validate(_read_from_row(row)) for row in rows]
    return PullRequestPage(items=items)


@router.get(
    "/pull-requests/{pull_request_id}",
    response_model=PullRequestDetail,
    summary="Get a pull request",
)
async def get_pull_request(pull_request_id: str, scoped: ScopedDep) -> PullRequestDetail:
    """Return one PR for the in-app desk, with worktree path and pending gates."""
    row = await PullRequestRepo(scoped.session).get_with_context(
        workspace_id=scoped.workspace_id,
        pull_request_id=pull_request_id,
    )
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "pull request not found")

    worktree_path: str | None = None
    worktree_branch: str | None = None
    if row.run.worktree_id is not None:
        worktree = await ScopedRepo(scoped.session, Worktree, scoped.workspace_id).get(
            row.run.worktree_id
        )
        if worktree is not None:
            worktree_path = worktree.path
            worktree_branch = worktree.branch

    approvals = await ApprovalRepo(scoped.session, Approval, scoped.workspace_id).list_inbox(
        status=ApprovalStatus.PENDING.value,
        run_id=row.run.id,
    )
    pending = [
        PullRequestPendingApproval(id=approval.id, kind=ApprovalKind(approval.kind))
        for approval in approvals
        if approval.kind in _PR_DESK_KINDS
    ]

    return PullRequestDetail.model_validate(
        {
            **_read_from_row(row),
            "worktree_path": worktree_path,
            "worktree_branch": worktree_branch,
            "pending_approvals": pending,
        }
    )
