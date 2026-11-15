"""Runs API (doc 14): start, list, inspect, cancel and diff a run."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, Request, Response, status

from gaia.api.deps import PrincipalDep, ScopedDep, ServicesDep, append_audit_log
from gaia.api.errors import ErrorCode, error_response
from gaia.db.engine import session_scope
from gaia.db.models import Repository, Run, Ticket, Worktree
from gaia.db.repo import RunRepo, RunSummaryRow, ScopedRepo
from gaia.domain.enums import RunStatus
from gaia.domain.schemas import (
    ApprovalRead,
    RunCreate,
    RunCurrentStep,
    RunDetail,
    RunPage,
    RunPullRequestRead,
    RunRead,
    RunStepRead,
    RunSummary,
    RunTicketSummary,
    RunWorktreeSummary,
)
from gaia.domain.state import IllegalTransition
from gaia.services.git import WorktreeLocation

router = APIRouter(tags=["runs"])

_TERMINAL_RUN_STATUSES = frozenset(
    {
        RunStatus.FAILED.value,
        RunStatus.COMPLETED.value,
        RunStatus.CANCELLED.value,
    }
)

_MARKER = "\n[diff truncated]"


def _orchestrator_unavailable(request: Request) -> Response:
    """The one 503 shape for every run/decision route."""
    return error_response(
        request,
        status.HTTP_503_SERVICE_UNAVAILABLE,
        ErrorCode.ORCHESTRATOR_UNAVAILABLE,
        "the orchestrator is unavailable",
    )


def _run_summary(row: RunSummaryRow) -> RunSummary:
    """Build a run summary from a :class:`~gaia.db.repo.RunSummaryRow`."""
    run = row.run
    ticket = row.ticket
    current_step_index = row.current_step_index
    current_step_title = row.current_step_title
    pr_number = row.pr_number
    pr_url = row.pr_url
    pr_ci_status = row.pr_ci_status
    return RunSummary.model_validate(
        {
            **RunRead.model_validate(run).model_dump(),
            "ticket_title": ticket.title,
            "current_step": (
                RunCurrentStep(index=current_step_index, title=current_step_title).model_dump()
                if current_step_index is not None and current_step_title is not None
                else None
            ),
            "pr_number": pr_number,
            "pr_url": pr_url,
            "ci_status": pr_ci_status,
        }
    )


def _truncate_diff(text: str, max_bytes: int, *, truncated: bool) -> str:
    """Return ``text`` capped at ``max_bytes`` and with a truncation marker."""
    marker_bytes = len(_MARKER.encode("utf-8"))
    if not truncated and len(text.encode("utf-8")) <= max_bytes:
        return text
    allowed = max_bytes - marker_bytes
    if allowed <= 0:
        return _MARKER.lstrip("\n")
    return text.encode("utf-8")[:allowed].decode("utf-8", errors="ignore") + _MARKER


@router.post(
    "/runs",
    response_model=RunRead,
    status_code=status.HTTP_201_CREATED,
    summary="Start a run",
)
async def start_run(
    payload: RunCreate,
    request: Request,
    scoped: ScopedDep,
    principal: PrincipalDep,
    services: ServicesDep,
) -> RunRead | Response:
    """Start the graph for ``ticket_id`` and return the new run row."""
    if services.runs is None:
        return _orchestrator_unavailable(request)

    ticket = await scoped.repo(Ticket).get(payload.ticket_id)
    if ticket is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "ticket not found")

    active = await scoped.repo(Run).list(ticket_id=payload.ticket_id)
    if any(run.status not in _TERMINAL_RUN_STATUSES for run in active):
        raise HTTPException(status.HTTP_409_CONFLICT, "ticket already has an active run")

    run_id = await services.runs.start(
        payload.ticket_id,
        workspace_id=scoped.workspace_id,
        created_by=principal.user_id,
        junior_runtime=payload.junior_runtime,
        budget_usd=payload.budget_usd,
    )
    # ``start`` commits on the manager's session; the request's session may have
    # opened a SQLite read transaction before that commit, so load the row in a
    # fresh session rather than from the request session.
    async with session_scope(services.sessions) as session:
        run = await ScopedRepo(session, Run, scoped.workspace_id).get(run_id)
    if run is None:
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, "run was not created")

    await append_audit_log(
        scoped.session,
        scoped.workspace_id,
        actor_user_id=principal.user_id,
        action="run.start",
        target=run_id,
        details={
            "ticket_id": payload.ticket_id,
            "junior_runtime": payload.junior_runtime.value
            if payload.junior_runtime is not None
            else None,
            "budget_usd": str(run.budget_usd),
        },
    )
    return RunRead.model_validate(run)


@router.get("/runs", response_model=RunPage, summary="List runs")
async def list_runs(
    request: Request,
    services: ServicesDep,
    scoped: ScopedDep,
    status_filter: Annotated[RunStatus | None, Query(alias="status")] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    cursor: Annotated[str | None, Query(max_length=36)] = None,
) -> RunPage | Response:
    """Return the workspace's runs, newest first."""
    if services.runs is None:
        return _orchestrator_unavailable(request)

    rows = await RunRepo(scoped.session).list_summary(
        scoped.workspace_id,
        status=status_filter.value if status_filter is not None else None,
        after_id=cursor,
        limit=limit + 1,
    )
    has_more = len(rows) > limit
    items = [_run_summary(row) for row in rows[:limit]]
    next_cursor = items[-1].id if has_more and items else None
    return RunPage(items=items, next_cursor=next_cursor)


@router.get("/runs/{run_id}", response_model=RunDetail, summary="Get a run")
async def get_run(
    run_id: str,
    request: Request,
    services: ServicesDep,
    scoped: ScopedDep,
) -> RunDetail | Response:
    """Return one run with its steps, worktree, approvals and PR."""
    if services.runs is None:
        return _orchestrator_unavailable(request)

    detail = await RunRepo(scoped.session).get_detail(scoped.workspace_id, run_id)
    if detail is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "run not found")

    pull_request = (
        RunPullRequestRead.model_validate(
            {
                "id": detail.pull_request.id,
                "number": detail.pull_request.number,
                "url": detail.pull_request.url,
                "state": detail.pull_request.state,
                "ci_status": detail.pull_request.ci_status,
                "review_state": detail.pull_request.review_state,
                "head_sha": detail.pull_request.head_sha,
                "created_at": detail.pull_request.created_at,
                "updated_at": detail.pull_request.updated_at,
            }
        )
        if detail.pull_request is not None
        else None
    )
    return RunDetail(
        run=RunRead.model_validate(detail.run),
        ticket=RunTicketSummary.model_validate(detail.ticket),
        steps=[RunStepRead.model_validate(step) for step in detail.steps],
        worktree=(
            RunWorktreeSummary.model_validate(detail.worktree)
            if detail.worktree is not None
            else None
        ),
        approvals=[ApprovalRead.model_validate(approval) for approval in detail.approvals],
        pull_request=pull_request,
    )


@router.post("/runs/{run_id}/cancel", response_model=RunRead, summary="Cancel a run")
async def cancel_run(
    run_id: str,
    request: Request,
    services: ServicesDep,
    scoped: ScopedDep,
    principal: PrincipalDep,
) -> RunRead | Response:
    """Cancel a live run, keep its worktree, and mark the run cancelled."""
    if services.runs is None:
        return _orchestrator_unavailable(request)

    try:
        await services.runs.cancel(run_id, workspace_id=scoped.workspace_id)
    except IllegalTransition:
        raise HTTPException(status.HTTP_409_CONFLICT, "run is already terminal") from None
    except ValueError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "run not found") from None

    async with session_scope(services.sessions) as session:
        run = await ScopedRepo(session, Run, scoped.workspace_id).get(run_id)
    if run is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "run not found")

    await append_audit_log(
        scoped.session,
        scoped.workspace_id,
        actor_user_id=principal.user_id,
        action="run.cancel",
        target=run_id,
        details={},
    )
    return RunRead.model_validate(run)


@router.get("/runs/{run_id}/diff", summary="Get a run diff")
async def get_run_diff(
    run_id: str,
    request: Request,
    services: ServicesDep,
    scoped: ScopedDep,
    max_bytes: Annotated[int, Query(ge=1024, le=2_097_152)] = 512_000,
) -> Response:
    """Return the committed plus uncommitted diff, capped at ``max_bytes``.

    Git runs after the database rows are loaded and the session is closed: a GET
    must not change git state, and no git call may sit inside a DB transaction.
    """
    if services.runs is None:
        return _orchestrator_unavailable(request)

    async with session_scope(services.sessions) as session:
        run = await ScopedRepo(session, Run, scoped.workspace_id).get(run_id)
        if run is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "run not found")
        if run.worktree_id is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "run has no worktree")
        worktree = await ScopedRepo(session, Worktree, scoped.workspace_id).get(run.worktree_id)
        if worktree is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "run has no worktree")
        repository = await ScopedRepo(session, Repository, scoped.workspace_id).get(run.repo_id)
        if repository is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "run has no repository")

    repo_path = await services.git.validate_repo(repository.local_path)
    if worktree.git_dir is not None:
        location = WorktreeLocation(Path(worktree.path), Path(worktree.git_dir))
    else:
        git_dir = await services.git.worktree_admin_dir(repo_path, Path(worktree.path))
        location = WorktreeLocation(Path(worktree.path), git_dir)

    if worktree.base_sha is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "run has no worktree base")

    committed = await services.git.diff(
        location,
        worktree.base_sha,
        "HEAD",
        max_output_bytes=max_bytes,
    )
    uncommitted = await services.git.diff_worktree(location, max_output_bytes=max_bytes)

    parts = [committed] if committed else []
    if uncommitted.stdout:
        parts.append(uncommitted.stdout)
    combined = "\n".join(parts)
    truncated = (
        len(committed.encode("utf-8")) >= max_bytes
        or uncommitted.stdout_truncated
        or len(uncommitted.stdout.encode("utf-8")) >= max_bytes
    )
    body = _truncate_diff(combined, max_bytes, truncated=truncated)
    return Response(content=body, media_type="text/x-diff; charset=utf-8")
