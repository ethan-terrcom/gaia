"""The in-process event bus and its SSE-facing replay/subscribe surface.

Owned by doc 13. The bus writes each event in its own short transaction before
fanning it out to subscribers. That is deliberate: an event records something
that happened even when the caller later rolls its own transaction back, so the
``event`` table is an append-only trail of what Gaia did, not of what the caller
kept.

``seq`` ordering is the order in which :meth:`EventBus.publish` commits. A single
``asyncio.Lock`` is held across append, commit and fan-out, so with SQLite's
single writer the sequence numbers are also the order subscribers observe (review
finding F9: revisit this assumption for concurrent Postgres writers).
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from datetime import datetime
from typing import Any, Protocol, cast

from pydantic import BaseModel, ConfigDict
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from gaia.db.engine import session_scope
from gaia.db.models import Event
from gaia.db.repo import EventRepo
from gaia.db.types import utcnow
from gaia.domain.enums import EventType
from gaia.security.redaction import redact_value


class EventRecord(BaseModel):
    """The wire format of one event, and what the bus hands subscribers.

    Persisted events always have an integer ``seq`` and an ``id``. A
    ``stream.gap`` marker is transport-only: it has ``seq=None`` and ``id=None``
    and is never written to the database.
    """

    model_config = ConfigDict(frozen=True)

    seq: int | None
    id: str | None
    workspace_id: str
    run_id: str | None
    agent_id: str | None
    type: EventType
    payload: dict[str, Any]
    ts: datetime


def event_record_from_event(event: Event) -> EventRecord:
    """Build the wire model from an :class:`~gaia.db.models.Event` row."""
    return EventRecord(
        seq=event.seq,
        id=event.id,
        workspace_id=event.workspace_id,
        run_id=event.run_id,
        agent_id=event.agent_id,
        type=EventType(event.type),
        payload=event.payload_json,
        ts=event.ts,
    )


def _truncate_payload(payload: Mapping[str, Any], max_field_bytes: int) -> dict[str, Any]:
    """Return ``payload`` with every string over ``max_field_bytes`` cut.

    The cut is by UTF-8 bytes, not characters, because proxies and browsers have
    byte-based limits. Dotted paths (``nested.0.field``) of every cut value are
    collected in the top-level ``_truncated`` key, so the UI can say *what* was
    shortened rather than silently showing a prefix.
    """
    truncated: list[str] = []

    def walk(value: Any, path: str) -> Any:
        if isinstance(value, str):
            encoded = value.encode("utf-8")
            if len(encoded) > max_field_bytes:
                truncated.append(path)
                return encoded[:max_field_bytes].decode("utf-8", errors="ignore")
            return value
        if isinstance(value, Mapping):
            return {
                key: walk(item, f"{path}.{key}" if path else key) for key, item in value.items()
            }
        if isinstance(value, list):
            return [
                walk(item, f"{path}.{index}" if path else str(index))
                for index, item in enumerate(value)
            ]
        return value

    result = cast(dict[str, Any], walk(dict(payload), ""))
    if truncated:
        result["_truncated"] = truncated
    return result


class EventSink(Protocol):
    """The narrow publisher seam runtimes and graph nodes depend on (doc 10/12)."""

    async def emit(self, type: EventType, payload: Mapping[str, Any]) -> None:
        """Publish one event to the bus."""
        ...


class BusEventSink:
    """Adapt an :class:`EventBus` to the :class:`EventSink` protocol."""

    def __init__(
        self,
        bus: EventBus,
        *,
        workspace_id: str,
        run_id: str | None = None,
        agent_id: str | None = None,
    ) -> None:
        self.bus = bus
        self.workspace_id = workspace_id
        self.run_id = run_id
        self.agent_id = agent_id

    async def emit(self, type: EventType, payload: Mapping[str, Any]) -> None:
        """Publish one event with the sink's fixed scoping."""
        await self.bus.publish(
            workspace_id=self.workspace_id,
            type=type,
            payload=payload,
            run_id=self.run_id,
            agent_id=self.agent_id,
        )


class Subscription:
    """One subscriber's live queue.

    The queue stores only real events, bounded by ``queue_size``. When it is
    full, the oldest event is dropped and a pending ``stream.gap`` marker is
    recorded; :meth:`__anext__` yields that marker before the next queued event.
    Keeping the gap out of the queue means the queue still holds the newest
    ``queue_size`` events, and repeated overflows collapse into one marker.
    """

    def __init__(
        self,
        bus: EventBus,
        *,
        workspace_id: str,
        run_id: str | None,
        queue_size: int,
    ) -> None:
        self.bus = bus
        self.workspace_id = workspace_id
        self.run_id = run_id
        self._queue: asyncio.Queue[EventRecord] = asyncio.Queue(maxsize=queue_size)
        self._gap_missed_from: int | None = None
        self._entered = False

    async def __aenter__(self) -> Subscription:
        self.bus._add_subscriber(self)
        self._entered = True
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: Any,
    ) -> bool:
        self.bus._remove_subscriber(self)
        self._entered = False
        return False

    def __aiter__(self) -> Subscription:
        return self

    async def __anext__(self) -> EventRecord:
        if self._gap_missed_from is not None:
            missed_from = self._gap_missed_from
            self._gap_missed_from = None
            return EventRecord(
                seq=None,
                id=None,
                workspace_id=self.workspace_id,
                run_id=self.run_id,
                agent_id=None,
                type=EventType.STREAM_GAP,
                payload={"missed_from": missed_from},
                ts=utcnow(),
            )
        return await self._queue.get()


class EventBus:
    """Publish, replay and subscribe to Gaia's event log (doc 13)."""

    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        *,
        queue_size: int = 1000,
        max_field_bytes: int = 8192,
    ) -> None:
        if queue_size < 1:
            raise ValueError("queue_size must be at least 1")
        self.sessions = sessions
        self.queue_size = queue_size
        self.max_field_bytes = max_field_bytes
        self._subscribers: dict[str, set[Subscription]] = {}
        self._lock = asyncio.Lock()

    async def publish(
        self,
        *,
        workspace_id: str,
        type: EventType,
        payload: Mapping[str, Any],
        run_id: str | None = None,
        agent_id: str | None = None,
    ) -> EventRecord:
        """Redact, truncate, persist and fan out one event.

        The insert is committed in its own transaction before fan-out, so a
        caller that later rolls back does not erase the trail. ``stream.gap`` is
        transport-only and is refused here.
        """
        if type is EventType.STREAM_GAP:
            raise ValueError("stream.gap is a transport-only marker and cannot be published")
        redacted = redact_value(dict(payload))
        truncated = _truncate_payload(redacted, self.max_field_bytes)
        async with self._lock:
            async with session_scope(self.sessions) as session:
                row = await EventRepo(session, Event, workspace_id).append(
                    type=type.value,
                    payload_json=truncated,
                    run_id=run_id,
                    agent_id=agent_id,
                )
            record = event_record_from_event(row)
            self._fanout(record)
        return record

    async def replay(
        self,
        workspace_id: str,
        run_id: str | None,
        after_seq: int,
        limit: int | None,
    ) -> list[EventRecord]:
        """Read events after ``after_seq`` from the database, oldest first."""
        async with self.sessions() as session:
            rows = await EventRepo(session, Event, workspace_id).list_after(
                after_seq=after_seq,
                limit=limit,
                run_id=run_id,
            )
        return [event_record_from_event(row) for row in rows]

    def subscribe(self, workspace_id: str, run_id: str | None) -> Subscription:
        """Return an async context manager/iterator for live events.

        Use as ``async with bus.subscribe(ws, run_id) as events:``. The
        subscription unregisters on exit, so a disconnected client cannot make
        the bus fan out to a dead queue forever.
        """
        return Subscription(
            self,
            workspace_id=workspace_id,
            run_id=run_id,
            queue_size=self.queue_size,
        )

    def _add_subscriber(self, subscription: Subscription) -> None:
        self._subscribers.setdefault(subscription.workspace_id, set()).add(subscription)

    def _remove_subscriber(self, subscription: Subscription) -> None:
        subscribers = self._subscribers.get(subscription.workspace_id)
        if subscribers is None:
            return
        subscribers.discard(subscription)
        if not subscribers:
            self._subscribers.pop(subscription.workspace_id, None)

    def _put_live(self, subscription: Subscription, record: EventRecord) -> None:
        """Enqueue ``record``, dropping the oldest live event on overflow.

        The dropped sequence becomes (or extends) the subscription's pending gap
        marker. A subscriber with a run filter only receives matching events
        because :meth:`_fanout` filters before calling this.
        """
        try:
            subscription._queue.put_nowait(record)
            return
        except asyncio.QueueFull:
            # The queue was full a moment ago and only this publisher removes
            # from it, so it is still non-empty.
            dropped = subscription._queue.get_nowait()
            if dropped.seq is not None:
                if subscription._gap_missed_from is None:
                    subscription._gap_missed_from = dropped.seq
                else:
                    subscription._gap_missed_from = min(subscription._gap_missed_from, dropped.seq)
            subscription._queue.put_nowait(record)

    def _fanout(self, record: EventRecord) -> None:
        """Deliver ``record`` to every matching subscriber, in queue order."""
        subscribers = self._subscribers.get(record.workspace_id)
        if subscribers is None:
            return
        for subscription in list(subscribers):
            if subscription.run_id is None or subscription.run_id == record.run_id:
                self._put_live(subscription, record)
