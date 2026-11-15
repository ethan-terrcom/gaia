"""List the pull requests Gaia opened, with run/ticket/repository context (doc 15b)."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query

from gaia.api.deps import ScopedDep
from gaia.db.repo import PullRequestRepo
from gaia.domain.enums import PullRequestState
from gaia.domain.schemas import PullRequestPage, PullRequestRead

router = APIRouter(tags=["pull-requests"])


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
    items = [
        PullRequestRead.model_validate(
            {
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
        )
        for row in rows
    ]
    return PullRequestPage(items=items)
