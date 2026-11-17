"""Approval inbox and the human decision endpoint (docs 14 and 16)."""

from __future__ import annotations

import asyncio
import hashlib
import json
from typing import Annotated, Any, Literal

from fastapi import APIRouter, HTTPException, Query, Request, Response, status
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from gaia.api.deps import PrincipalDep, ScopedDep, ServicesDep, append_audit_log
from gaia.api.errors import ErrorCode, error_response
from gaia.db.engine import session_scope
from gaia.db.models import Approval, PullRequest, Run
from gaia.db.repo import ApprovalRepo, ScopedRepo
from gaia.domain.enums import ApprovalKind, ApprovalStatus
from gaia.domain.plans import Plan
from gaia.domain.schemas import (
    ApprovalPage,
    ApprovalRead,
    DecideAccepted,
    DecideRequest,
)
from gaia.orchestrator.approvals import Decision
from gaia.orchestrator.guard import TERMINAL_RUN_STATUSES
from gaia.orchestrator.pause import OrchestratorPaused
from gaia.orchestrator.runner import RunBusy, RunManager
from gaia.runtimes.approvals import ApprovalNotPending, DbApprovalBroker
from gaia.security.commands import suggest_allowed_prefix
from gaia.services.allowed_commands import remember

router = APIRouter(tags=["approvals"])

#: Per-approval locks. SQLite has no ``SELECT ... FOR UPDATE``, so two
#: concurrent decisions are serialised here instead (deviation, doc 14).
_DECIDE_LOCKS: dict[str, asyncio.Lock] = {}
_DECIDE_LOCKS_GUARD = asyncio.Lock()


def _unavailable(request: Request) -> Response:
    """The 503 answer when the orchestrator/broker were not built."""
    return error_response(
        request,
        status.HTTP_503_SERVICE_UNAVAILABLE,
        ErrorCode.ORCHESTRATOR_UNAVAILABLE,
        "the orchestrator is unavailable",
    )


def _orchestrator_paused(request: Request) -> Response:
    """The 409 answer while the global kill switch is on."""
    return error_response(
        request,
        status.HTTP_409_CONFLICT,
        ErrorCode.ORCHESTRATOR_PAUSED,
        "the orchestrator is paused",
    )


def _approval_not_pending(request: Request, approval_id: str, approval_status: str) -> Response:
    return error_response(
        request,
        status.HTTP_409_CONFLICT,
        ErrorCode.APPROVAL_NOT_PENDING,
        f"approval {approval_id!r} is {approval_status}, not pending",
    )


def _run_busy(request: Request, run_id: str) -> Response:
    return error_response(
        request,
        status.HTTP_409_CONFLICT,
        ErrorCode.RUN_BUSY,
        f"run {run_id!r} is already executing",
    )


def _run_not_active(request: Request, run_id: str, run_status: str) -> Response:
    return error_response(
        request,
        status.HTTP_409_CONFLICT,
        ErrorCode.RUN_NOT_ACTIVE,
        f"run {run_id!r} is {run_status}; its approvals can no longer be decided",
    )


def _merge_confirmation_required(request: Request, pr_number: int | None) -> Response:
    message = (
        "the run has no pull request to merge"
        if pr_number is None
        else f"confirm must be 'merge {pr_number}'"
    )
    return error_response(
        request,
        status.HTTP_422_UNPROCESSABLE_CONTENT,
        ErrorCode.MERGE_CONFIRMATION_REQUIRED,
        message,
    )


async def _approval_lock(approval_id: str) -> asyncio.Lock:
    """Return the process-wide lock for ``approval_id``."""
    async with _DECIDE_LOCKS_GUARD:
        lock = _DECIDE_LOCKS.get(approval_id)
        if lock is None:
            lock = asyncio.Lock()
            _DECIDE_LOCKS[approval_id] = lock
        return lock


def _edited_payload_sha256(payload: dict[str, Any] | None) -> str | None:
    """The audit-details placeholder for an edited plan payload."""
    if payload is None:
        return None
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


@router.get("/approvals", response_model=ApprovalPage, summary="List approval requests")
async def list_approvals(
    scoped: ScopedDep,
    status_filter: Annotated[ApprovalStatus | None, Query(alias="status")] = None,
    run_id: Annotated[str | None, Query(max_length=36)] = None,
    decided: Annotated[
        bool, Query(description="Only approvals that are no longer pending.")
    ] = False,
    order: Annotated[Literal["oldest", "newest"], Query()] = "oldest",
    limit: Annotated[int, Query(ge=1, le=200)] = 100,
) -> ApprovalPage:
    """Return approvals, oldest first by default, optionally filtered.

    ``decided=true`` with ``order=newest`` is the inbox's "recent decisions"
    view (lead review of 17d: oldest-first made it show the oldest 50).

    ``total_pending`` always counts the workspace's pending approvals, whatever
    filters the caller passed: the badge makes one call and must not change with
    the page. ``ApprovalRepo`` keeps every read inside the authenticated
    workspace.
    """
    approvals = ApprovalRepo(scoped.session, Approval, scoped.workspace_id)
    rows = await approvals.list_inbox(
        status=status_filter.value if status_filter is not None else None,
        run_id=run_id,
        decided=decided,
        newest_first=order == "newest",
        limit=limit,
    )
    return ApprovalPage(
        items=[ApprovalRead.model_validate(row) for row in rows],
        total_pending=await approvals.count_pending(),
    )


@router.post(
    "/approvals/{approval_id}/decide",
    response_model=None,
    responses={
        status.HTTP_200_OK: {"model": ApprovalRead},
        status.HTTP_202_ACCEPTED: {"model": DecideAccepted},
    },
    summary="Decide an approval",
)
async def decide_approval(
    approval_id: str,
    payload: DecideRequest,
    request: Request,
    scoped: ScopedDep,
    principal: PrincipalDep,
    services: ServicesDep,
) -> Response:
    """Record a human decision and, for graph gates, resume the run.

    Command approvals are answered inside the runtime's turn and do not resume
    the graph (doc 14). Graph-gate decisions are applied asynchronously by the
    resumed node, so this route returns 202 and never marks the row decided
    itself.
    """
    runs = services.runs
    approvals = services.approvals
    if runs is None or approvals is None:
        return _unavailable(request)

    lock = await _approval_lock(approval_id)
    async with lock:
        try:
            return await _decide_locked(
                approval_id,
                payload,
                request,
                scoped,
                principal,
                services,
                runs,
                approvals,
            )
        finally:
            async with _DECIDE_LOCKS_GUARD:
                if _DECIDE_LOCKS.get(approval_id) is lock:
                    del _DECIDE_LOCKS[approval_id]


async def _decide_locked(
    approval_id: str,
    payload: DecideRequest,
    request: Request,
    scoped: ScopedDep,
    principal: PrincipalDep,
    services: ServicesDep,
    runs: RunManager,
    approvals: DbApprovalBroker,
) -> Response:
    """The actual decision, called while holding ``approval_id``'s lock."""
    approval = await scoped.repo(Approval).get(approval_id)
    if approval is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "approval not found")
    if approval.status != ApprovalStatus.PENDING.value:
        return _approval_not_pending(request, approval_id, approval.status)
    # A finished run acts on nothing: deciding would resume a graph that ended
    # (found in the first live end-to-end run). Lead review fix B.
    run = await scoped.repo(Run).get(approval.run_id)
    if run is not None and run.status in TERMINAL_RUN_STATUSES:
        return _run_not_active(request, approval.run_id, run.status)

    try:
        kind = ApprovalKind(approval.kind)
    except ValueError:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "unknown approval kind",
        ) from None

    # The kill switch blocks graph-gate decisions (they would resume a graph),
    # but command approvals still work: the agent is gone anyway, and the broker
    # simply reports the decision to the runtime.
    if kind != ApprovalKind.COMMAND and services.pause is not None and services.pause.is_paused():
        return _orchestrator_paused(request)

    details: dict[str, Any] = {
        "kind": kind.value,
        "decision": payload.decision,
    }
    if payload.edited_payload is not None:
        details["edited_payload_sha256"] = _edited_payload_sha256(payload.edited_payload)
    if payload.remember and (kind != ApprovalKind.COMMAND or payload.decision != "approve"):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "remember is only valid when approving a command",
        )

    if kind == ApprovalKind.COMMAND:
        if payload.decision == "edit":
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                "edit is not valid for command approvals",
            )
        remembered: tuple[list[str], bool] | None = None
        if payload.remember:
            remembered = await _remember_command(approval, run, scoped, services)
            details["remembered"] = remembered[0]
        try:
            await approvals.decide(
                scoped.workspace_id,
                approval_id,
                approved=payload.decision == "approve",
                comment=payload.comment or "",
                user_id=principal.user_id,
            )
        except ApprovalNotPending:
            return _approval_not_pending(request, approval_id, approval.status)
        except LookupError:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "approval not found") from None

        await append_audit_log(
            scoped.session,
            scoped.workspace_id,
            actor_user_id=principal.user_id,
            action="approval.decide",
            target=approval_id,
            details=details,
        )
        if remembered is not None and run is not None:
            # Written after the broker's own session committed: holding a write
            # in this request's session first would lock SQLite against it.
            await append_audit_log(
                scoped.session,
                scoped.workspace_id,
                actor_user_id=principal.user_id,
                action="repository.allow_command",
                target=run.repo_id,
                details={"argv": remembered[0], "added": remembered[1], "approval_id": approval_id},
            )
        async with session_scope(services.sessions) as session:
            row = await ScopedRepo(session, Approval, scoped.workspace_id).get(approval_id)
        if row is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "approval not found")
        return JSONResponse(
            content=ApprovalRead.model_validate(row).model_dump(mode="json"),
            status_code=status.HTTP_200_OK,
        )

    decision_payload: dict[str, Any] = {
        "action": payload.decision,
        "comment": payload.comment,
        "plan": None,
        "budget_usd": None,
        "decided_by": principal.user_id,
    }

    if payload.decision == "edit":
        if kind != ApprovalKind.PLAN:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                "edit is only valid for plan approvals",
            )
        try:
            plan = Plan.model_validate(payload.edited_payload)
        except ValidationError as exc:
            first_error = exc.errors()[0]
            location = (
                ".".join(str(part) for part in first_error.get("loc", ())) or "edited_payload"
            )
            message = f"{location}: {first_error.get('msg', 'invalid value')}"
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, message) from None
        decision_payload["plan"] = plan.model_dump(mode="json")
    elif payload.decision == "approve" and kind == ApprovalKind.BUDGET:
        decision_payload["budget_usd"] = payload.budget_usd

    if kind == ApprovalKind.MERGE and payload.decision == "approve":
        pr_number = await _merge_pr_number(approval, scoped)
        if pr_number is None:
            return _merge_confirmation_required(request, None)
        if payload.confirm != f"merge {pr_number}":
            return _merge_confirmation_required(request, pr_number)

    decision = Decision.model_validate(decision_payload)
    try:
        await runs.resume(approval.run_id, decision.model_dump(mode="json"))
    except OrchestratorPaused:
        return _orchestrator_paused(request)
    except RunBusy:
        return _run_busy(request, approval.run_id)

    await append_audit_log(
        scoped.session,
        scoped.workspace_id,
        actor_user_id=principal.user_id,
        action="approval.decide",
        target=approval_id,
        details=details,
    )
    return JSONResponse(
        content=DecideAccepted(
            approval_id=approval_id,
            status="accepted",
        ).model_dump(mode="json"),
        status_code=status.HTTP_202_ACCEPTED,
    )


async def _merge_pr_number(approval: Approval, scoped: ScopedDep) -> int | None:
    """Return the PR number for a merge approval.

    The 12a ``approve_merge`` node currently stores only ``kind`` in
    ``payload_json`` (deviation: doc 14 says to read the payload). Fall back to
    the run's latest pull-request row so the confirmation can still be typed.
    """
    payload_number = approval.payload_json.get("pr_number")
    if payload_number is not None:
        return int(payload_number)
    pull_requests = ScopedRepo(scoped.session, PullRequest, scoped.workspace_id)
    rows = await pull_requests.list(run_id=approval.run_id)
    if not rows:
        return None
    latest = max(rows, key=lambda pr: (pr.created_at, pr.id))
    return latest.number


async def _remember_command(
    approval: Approval,
    run: Run | None,
    scoped: ScopedDep,
    services: ServicesDep,
) -> tuple[list[str], bool]:
    """Add the approved command's prefix to its repository's allowed commands.

    Returns the prefix and whether it was new; the caller audits it.

    The prefix is recomputed from the stored ``argv`` here, never taken from
    the request, and committed before the agent is woken so its next command
    already runs without asking.
    """
    argv = approval.payload_json.get("argv") if isinstance(approval.payload_json, dict) else None
    prefix = suggest_allowed_prefix(argv) if isinstance(argv, list) else None
    if prefix is None or run is None:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "this command cannot be added to the allowed commands; approve it without remembering",
        )
    added = await remember(services.sessions, scoped.workspace_id, run.repo_id, prefix)
    return list(prefix), added
