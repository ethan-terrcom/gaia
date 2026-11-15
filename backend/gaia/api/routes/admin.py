"""Global kill-switch routes (doc 18a).

Pause is global, not per workspace: Gaia is single-user and local. The routes
are still mounted on the authenticated ``/api/v1`` router, so they require the
same bearer token as every other route.
"""

from __future__ import annotations

from fastapi import APIRouter, Request, Response, status

from gaia.api.deps import PrincipalDep, ScopedDep, ServicesDep, append_audit_log
from gaia.api.errors import ErrorCode, error_response
from gaia.domain.schemas import PauseAllRequest, PauseAllResult, PauseStatus, ResumeAllResult

router = APIRouter(tags=["admin"])


def _unavailable(request: Request) -> Response:
    """The 503 answer when the orchestrator (or its pause switch) was not built."""
    return error_response(
        request,
        status.HTTP_503_SERVICE_UNAVAILABLE,
        ErrorCode.ORCHESTRATOR_UNAVAILABLE,
        "the orchestrator is unavailable",
    )


@router.get("/admin/pause", response_model=PauseStatus, summary="Read the global pause state")
async def get_pause(request: Request, services: ServicesDep) -> PauseStatus | Response:
    """Return whether the orchestrator is paused, and since when."""
    if services.runs is None or services.pause is None:
        return _unavailable(request)
    info = services.pause.info()
    return PauseStatus(
        paused=info is not None,
        since=info.since if info is not None else None,
        reason=info.reason if info is not None else None,
    )


@router.post(
    "/admin/pause-all",
    response_model=PauseAllResult,
    summary="Pause every run (global kill switch)",
)
async def pause_all(
    payload: PauseAllRequest,
    request: Request,
    scoped: ScopedDep,
    principal: PrincipalDep,
    services: ServicesDep,
) -> PauseAllResult | Response:
    """Cancel every live graph task and pause every queued run.

    Idempotent: pausing again returns the runs it paused this time, which is
    usually an empty list.
    """
    if services.runs is None or services.pause is None:
        return _unavailable(request)
    reason = payload.reason or "paused by user"
    paused_run_ids = await services.runs.pause_all(reason=reason)
    await append_audit_log(
        scoped.session,
        scoped.workspace_id,
        actor_user_id=principal.user_id,
        action="admin.pause_all",
        details={"reason": reason, "paused_run_ids": paused_run_ids},
    )
    return PauseAllResult(paused_run_ids=paused_run_ids)


@router.post(
    "/admin/resume-all",
    response_model=ResumeAllResult,
    summary="Resume every paused run",
)
async def resume_all(
    request: Request,
    scoped: ScopedDep,
    principal: PrincipalDep,
    services: ServicesDep,
) -> ResumeAllResult | Response:
    """Clear the global flag and relaunch every run it paused."""
    if services.runs is None or services.pause is None:
        return _unavailable(request)
    resumed_run_ids = await services.runs.resume_all()
    await append_audit_log(
        scoped.session,
        scoped.workspace_id,
        actor_user_id=principal.user_id,
        action="admin.resume_all",
        details={"resumed_run_ids": resumed_run_ids},
    )
    return ResumeAllResult(resumed_run_ids=resumed_run_ids)
