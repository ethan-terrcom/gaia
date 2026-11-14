"""Dev-only routes (doc 13 acceptance criterion).

Only ``create_app`` decides whether to mount this router, and it does so only
when ``Settings.dev_routes`` is true. The fake-run publisher exists because live
fan-out needs the server's own :class:`~gaia.services.events.EventBus`; a script
outside the server process cannot publish to it in-process.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import structlog
from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field

from gaia.api.auth import Principal
from gaia.api.deps import AppServices, PrincipalDep, ServicesDep
from gaia.db.engine import session_scope
from gaia.db.ids import uuid7
from gaia.db.models import Repository, Run, Ticket
from gaia.db.repo import ScopedRepo
from gaia.domain.enums import EventType, RunStatus
from gaia.services.events import EventBus

logger = structlog.get_logger(__name__)

router = APIRouter(tags=["dev"])


class FakeRunRequest(BaseModel):
    """A scripted run for exercising SSE while developing the UI.

    ``events`` is the number of ``agent.message`` frames; the route always adds
    the run-status, tool-call, tool-result and step-finished frames around them.
    """

    run_id: str | None = None
    events: int = Field(default=3, ge=1, le=100)
    interval_s: float = Field(default=0.2, gt=0, le=10)


class FakeRunResponse(BaseModel):
    """The id of the run being scripted."""

    run_id: str


async def _find_or_create_run(
    services: AppServices,
    principal: Principal,
    run_id: str | None,
) -> Run:
    """Return the requested run or create a scratch repo/ticket/run.

    The rows are committed in their own short transaction before the background
    publisher starts, so the event inserts always see a committed parent run.
    """
    if run_id is not None:
        async with session_scope(services.sessions) as session:
            run = await ScopedRepo(session, Run, principal.workspace_id).get(run_id)
        if run is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "run not found")
        return run

    async with session_scope(services.sessions) as session:
        repos = ScopedRepo(session, Repository, principal.workspace_id)
        repository = await repos.create(
            name=f"dev-fake-run-{uuid7()}",
            local_path=str(Path(services.settings.data_dir) / "dev" / f"fake-run-{uuid7()}"),
            created_by=principal.user_id,
        )
        tickets = ScopedRepo(session, Ticket, principal.workspace_id)
        ticket = await tickets.create(
            repo_id=repository.id,
            title="Dev fake run",
            created_by=principal.user_id,
        )
        runs = ScopedRepo(session, Run, principal.workspace_id)
        return await runs.create(
            ticket_id=ticket.id,
            repo_id=repository.id,
            created_by=principal.user_id,
        )


async def _publish_fake_run(
    bus: EventBus,
    *,
    workspace_id: str,
    run_id: str,
    events: int,
    interval_s: float,
) -> None:
    """Publish the scripted sequence until cancelled or finished."""
    try:
        await bus.publish(
            workspace_id=workspace_id,
            type=EventType.RUN_STATUS_CHANGED,
            payload={"status": RunStatus.IMPLEMENTING.value},
            run_id=run_id,
        )
        for index in range(events):
            await asyncio.sleep(interval_s)
            await bus.publish(
                workspace_id=workspace_id,
                type=EventType.AGENT_MESSAGE,
                payload={"index": index, "content": f"fake message {index}"},
                run_id=run_id,
            )
        await asyncio.sleep(interval_s)
        await bus.publish(
            workspace_id=workspace_id,
            type=EventType.AGENT_TOOL_CALL,
            payload={"name": "run_pytest", "arguments": {"argv": ["pytest", "-q"]}},
            run_id=run_id,
        )
        await asyncio.sleep(interval_s)
        await bus.publish(
            workspace_id=workspace_id,
            type=EventType.AGENT_TOOL_RESULT,
            payload={"tool_call_id": "dev-fake", "summary": "ok"},
            run_id=run_id,
        )
        await asyncio.sleep(interval_s)
        await bus.publish(
            workspace_id=workspace_id,
            type=EventType.STEP_FINISHED,
            payload={"step_index": 0},
            run_id=run_id,
        )
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("dev.fake_run_failed", run_id=run_id)
        raise


@router.post(
    "/dev/fake-run",
    response_model=FakeRunResponse,
    summary="Publish a scripted fake run (dev routes only)",
)
async def start_fake_run(
    payload: FakeRunRequest,
    services: ServicesDep,
    principal: PrincipalDep,
) -> FakeRunResponse:
    """Create a run if none is given, then publish events in a background task."""
    run = await _find_or_create_run(services, principal, payload.run_id)
    task = asyncio.create_task(
        _publish_fake_run(
            services.events,
            workspace_id=principal.workspace_id,
            run_id=run.id,
            events=payload.events,
            interval_s=payload.interval_s,
        )
    )
    services.background_tasks.add(task)
    task.add_done_callback(services.background_tasks.discard)
    return FakeRunResponse(run_id=run.id)
