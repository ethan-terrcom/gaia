"""Event stream endpoints (doc 13).

Two routers live here on purpose:

- ``router`` is mounted inside ``api_v1_router()`` and is bearer-authenticated.
  It issues the single-use stream tickets and serves the paginated run history.
- ``public_router`` is mounted directly in :func:`gaia.api.app.create_app` next
  to the health router. It serves ``GET /api/v1/stream`` with the full path and
  authenticates by the stream ticket alone, because a browser ``EventSource``
  cannot send an ``Authorization`` header.

Resume point: the ``Last-Event-ID`` header wins over the ``last_event_id`` query
parameter. The query form exists because the ticket is single use, so a browser
``EventSource`` cannot simply auto-reconnect with the same ticket; the UI
(doc 17) fetches a fresh ticket and reconnects with ``?last_event_id=``.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Callable
from typing import Annotated, Final

from fastapi import APIRouter, Header, HTTPException, Query, Request, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from gaia.api.deps import PrincipalDep, ScopedDep, ServicesDep, services_of
from gaia.db.engine import session_scope
from gaia.db.models import Event, Run
from gaia.db.repo import EventRepo, ScopedRepo
from gaia.domain.enums import EventType
from gaia.services.events import EventBus, EventRecord, event_record_from_event

router = APIRouter(tags=["stream"])
public_router = APIRouter(tags=["stream"])

#: How often the SSE route sends a ``: ping`` comment. Tests may override this
#: module constant or pass a shorter interval directly to :func:`sse_frames`.
HEARTBEAT_SECONDS: Final[float] = 15.0

#: Test-only bound for the otherwise endless SSE body. ``httpx.ASGITransport``
#: buffers the whole response, so an unbounded stream would hang a test; a test
#: monkeypatches this to a small integer to make the HTTP route return.
SSE_MAX_EVENTS: int | None = None

#: Rows read per database round trip when replaying. Replays are paged so a
#: reconnect after a long absence never loads a run's whole history at once.
REPLAY_PAGE_SIZE: Final = 500

#: Headers for the SSE response. ``Cache-Control`` is set explicitly here
#: because the security middleware uses ``setdefault`` and this route wants
#: ``no-cache`` rather than the API default ``no-store``.
SSE_HEADERS: Final = {
    "Cache-Control": "no-cache",
    "X-Accel-Buffering": "no",
}


class StreamTicket(BaseModel):
    """The short-lived credential the UI puts in the SSE query string."""

    ticket: str
    expires_in: float


class EventHistory(BaseModel):
    """One page of a run's event history."""

    items: list[EventRecord]
    next_after: int | None


@router.post(
    "/stream/ticket",
    response_model=StreamTicket,
    summary="Issue a single-use stream ticket",
)
async def create_stream_ticket(
    principal: PrincipalDep,
    services: ServicesDep,
) -> StreamTicket:
    """Exchange the bearer token for a short-lived SSE ticket."""
    ticket = services.stream_tickets.issue(principal)
    return StreamTicket(ticket=ticket, expires_in=services.stream_tickets.ttl_s)


@router.get(
    "/runs/{run_id}/events",
    response_model=EventHistory,
    summary="List a run's events",
)
async def list_run_events(
    run_id: str,
    scoped: ScopedDep,
    after: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> EventHistory:
    """Return events after ``after`` for one run, oldest first.

    The run is loaded through ``ScopedRepo``, so a run in another workspace is
    exactly the same 404 as a missing one.
    """
    run = await scoped.repo(Run).get(run_id)
    if run is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "run not found")
    events = EventRepo(scoped.session, Event, scoped.workspace_id)
    rows = await events.list_for_run(run_id, after_seq=after, limit=limit + 1)
    items = [event_record_from_event(row) for row in rows[:limit]]
    next_after = items[-1].seq if len(rows) > limit and items else None
    return EventHistory(items=items, next_after=next_after)


@public_router.get(
    "/api/v1/stream",
    summary="Server-sent event stream",
)
async def stream_events(
    request: Request,
    ticket: Annotated[str | None, Query()] = None,
    run_id: Annotated[str | None, Query(max_length=36)] = None,
    last_event_id: Annotated[int | None, Query(alias="last_event_id", ge=0)] = None,
    last_event_id_header: Annotated[int | None, Header(alias="Last-Event-ID", ge=0)] = None,
) -> StreamingResponse:
    """Stream events with ``text/event-stream``.

    A missing, used or expired ticket is the same 401 envelope; the route never
    distinguishes them to a client. The ``Last-Event-ID`` header wins over the
    ``last_event_id`` query parameter (see the module docstring for why both
    exist).
    """
    services = services_of(request)
    principal = services.stream_tickets.redeem(ticket or "")
    if principal is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "a valid stream ticket is required")
    if run_id is not None:
        async with session_scope(services.sessions) as session:
            run = await ScopedRepo(session, Run, principal.workspace_id).get(run_id)
        if run is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "run not found")
    # No resume point means "live from now": history has its own paginated
    # route (GET /runs/{id}/events), and replaying a workspace's whole log to
    # every new connection would be unbounded.
    last_seq = last_event_id_header if last_event_id_header is not None else last_event_id
    frames = sse_frames(
        services.events,
        workspace_id=principal.workspace_id,
        run_id=run_id,
        last_seq=last_seq,
        heartbeat_s=HEARTBEAT_SECONDS,
        is_disconnected=request.is_disconnected,
        max_events=SSE_MAX_EVENTS,
    )
    return StreamingResponse(frames, media_type="text/event-stream", headers=dict(SSE_HEADERS))


async def sse_frames(
    bus: EventBus,
    *,
    workspace_id: str,
    run_id: str | None,
    last_seq: int | None,
    heartbeat_s: float,
    is_disconnected: Callable[[], Awaitable[bool]],
    max_events: int | None = None,
) -> AsyncGenerator[str, None]:
    """Build the SSE body for one subscriber, without any HTTP dependency.

    Ordering without loss or duplicates: subscribe **first**, then replay from
    the database everything after ``last_seq`` (in pages), then drain the live
    queue, skipping any event whose ``seq`` was already sent. An event committed
    after subscription but before the replay query therefore arrives exactly once.

    ``last_seq=None`` means "live from now": nothing is replayed.

    A ``stream.gap`` from the bus (this subscriber fell more than ``queue_size``
    events behind) is healed here rather than forwarded: the missed range is
    replayed from the database, so a slow client never has to notice. The client
    only reconnects when the connection itself drops.
    """
    emitted = 0
    async with bus.subscribe(workspace_id, run_id) as subscription:
        last_sent = last_seq
        if last_seq is not None:
            async for event in _replay_after(bus, workspace_id, run_id, last_seq):
                if max_events is not None and emitted >= max_events:
                    return
                yield _sse_frame(event)
                emitted += 1
                last_sent = event.seq

        while max_events is None or emitted < max_events:
            if await is_disconnected():
                return
            try:
                event = await asyncio.wait_for(anext(subscription), timeout=heartbeat_s)
            except TimeoutError:
                yield ": ping\n\n"
                continue
            if event.type is EventType.STREAM_GAP:
                missed_from = int(event.payload["missed_from"])
                after = last_sent if last_sent is not None else missed_from - 1
                async for missed in _replay_after(bus, workspace_id, run_id, after):
                    if max_events is not None and emitted >= max_events:
                        return
                    yield _sse_frame(missed)
                    emitted += 1
                    last_sent = missed.seq
                continue
            if last_sent is not None and event.seq is not None and event.seq <= last_sent:
                continue
            yield _sse_frame(event)
            emitted += 1
            last_sent = event.seq


async def _replay_after(
    bus: EventBus, workspace_id: str, run_id: str | None, after_seq: int
) -> AsyncIterator[EventRecord]:
    """Yield persisted events after ``after_seq``, one page at a time."""
    while True:
        page = await bus.replay(workspace_id, run_id, after_seq, REPLAY_PAGE_SIZE)
        for event in page:
            yield event
        if len(page) < REPLAY_PAGE_SIZE:
            return
        last = page[-1].seq
        # Every persisted row has a seq; a page ending on None would be a bug.
        assert last is not None  # noqa: S101 - invariant of replay(), not input validation
        after_seq = last


def _sse_frame(event: EventRecord) -> str:
    """Render one SSE event: optional id, event type, data JSON, blank line."""
    lines: list[str] = []
    if event.seq is not None:
        lines.append(f"id: {event.seq}")
    lines.append(f"event: {event.type.value}")
    lines.append(f"data: {event.model_dump_json()}")
    return "\n".join(lines) + "\n\n"
