"""Tests for the in-process event bus (doc 13)."""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from gaia.db.models import Event
from gaia.db.repo import EventRepo
from gaia.db.types import utcnow
from gaia.domain.enums import EventType
from gaia.services.events import (
    BusEventSink,
    EventBus,
    EventRecord,
    event_record_from_event,
)
from tests.conftest import Database
from tests.db_builders import make_chain

DUMMY_SECRET = "dummy" + "-event-secret"


async def test_publish_reaches_a_subscriber(database: Database, session: AsyncSession) -> None:
    chain = await make_chain(session, slug="publish")
    await session.commit()
    bus = EventBus(database.sessions)

    async with bus.subscribe(chain.workspace.id, None) as subscription:
        published = await bus.publish(
            workspace_id=chain.workspace.id,
            type=EventType.RUN_STATUS_CHANGED,
            payload={"status": "implementing"},
            run_id=chain.run.id,
        )
        received = await subscription.__anext__()

    assert received == published


async def test_workspace_and_run_filters(database: Database, session: AsyncSession) -> None:
    alpha = await make_chain(session, slug="filter-alpha")
    beta = await make_chain(session, slug="filter-beta")
    await session.commit()
    bus = EventBus(database.sessions)

    async with bus.subscribe(alpha.workspace.id, alpha.run.id) as subscription:
        await bus.publish(
            workspace_id=beta.workspace.id,
            type=EventType.RUN_CREATED,
            payload={"foreign": True},
            run_id=beta.run.id,
        )
        await bus.publish(
            workspace_id=alpha.workspace.id,
            type=EventType.WORKTREE_CREATED,
            payload={"same_workspace": True},
        )
        matching = await bus.publish(
            workspace_id=alpha.workspace.id,
            type=EventType.STEP_FINISHED,
            payload={"matching": True},
            run_id=alpha.run.id,
        )
        received = await subscription.__anext__()

    assert received == matching


async def test_overflow_yields_exactly_one_gap_and_keeps_the_newest_events(
    database: Database, session: AsyncSession
) -> None:
    chain = await make_chain(session, slug="overflow")
    await session.commit()
    bus = EventBus(database.sessions, queue_size=3)

    async with bus.subscribe(chain.workspace.id, None) as subscription:
        for index in range(5):
            await bus.publish(
                workspace_id=chain.workspace.id,
                type=EventType.AGENT_MESSAGE,
                payload={"index": index},
                run_id=chain.run.id,
            )
        records = [await subscription.__anext__() for _ in range(4)]

    gap = records[0]
    assert gap.type is EventType.STREAM_GAP
    assert gap.seq is None
    assert gap.id is None
    assert gap.payload == {"missed_from": 1}
    assert [record.seq for record in records[1:]] == [3, 4, 5]
    assert sum(record.type is EventType.STREAM_GAP for record in records) == 1


async def test_replay_is_ordered_by_seq_and_scoped(
    database: Database, session: AsyncSession
) -> None:
    alpha = await make_chain(session, slug="replay-alpha")
    beta = await make_chain(session, slug="replay-beta")
    await session.commit()
    bus = EventBus(database.sessions)
    first = await bus.publish(
        workspace_id=alpha.workspace.id,
        type=EventType.RUN_CREATED,
        payload={"index": 0},
        run_id=alpha.run.id,
    )
    second = await bus.publish(
        workspace_id=alpha.workspace.id,
        type=EventType.RUN_STATUS_CHANGED,
        payload={"index": 1},
        run_id=alpha.run.id,
    )
    await bus.publish(
        workspace_id=beta.workspace.id,
        type=EventType.RUN_CREATED,
        payload={"foreign": True},
        run_id=beta.run.id,
    )

    replayed = await bus.replay(alpha.workspace.id, None, after_seq=0, limit=10)

    assert [record.seq for record in replayed] == [first.seq, second.seq]

    assert first.seq is not None
    run_replay = await bus.replay(alpha.workspace.id, alpha.run.id, after_seq=first.seq, limit=1)
    assert [record.seq for record in run_replay] == [second.seq]


async def test_publish_refuses_the_transport_only_gap_marker(
    database: Database, session: AsyncSession
) -> None:
    chain = await make_chain(session, slug="no-gap")
    await session.commit()
    bus = EventBus(database.sessions)

    with pytest.raises(ValueError, match="transport-only"):
        await bus.publish(
            workspace_id=chain.workspace.id,
            type=EventType.STREAM_GAP,
            payload={},
        )


async def test_publish_redacts_and_truncates_the_row_and_the_frame(
    database: Database, session: AsyncSession
) -> None:
    chain = await make_chain(session, slug="redact-truncate")
    await session.commit()
    from gaia.security.redaction import register_secret

    register_secret(DUMMY_SECRET)
    bus = EventBus(database.sessions, max_field_bytes=16)
    long_value = "z" * 40

    async with bus.subscribe(chain.workspace.id, None) as subscription:
        await bus.publish(
            workspace_id=chain.workspace.id,
            type=EventType.AGENT_MESSAGE,
            payload={
                "secret": DUMMY_SECRET,
                "nested": {"long": long_value},
                "list": [{"short": "ok"}],
            },
            run_id=chain.run.id,
        )
        received = await subscription.__anext__()

    assert received.payload["secret"] == "[REDACTED]"
    assert received.payload["_truncated"] == ["nested.long"]
    assert len(received.payload["nested"]["long"].encode("utf-8")) <= 16

    async with database.sessions() as check_session:
        rows = await EventRepo(check_session, Event, chain.workspace.id).list_after(limit=1)
    stored = rows[0].payload_json
    assert stored["secret"] == "[REDACTED]"
    assert stored["_truncated"] == ["nested.long"]
    assert stored["nested"]["long"] != long_value

    from gaia.api.routes.stream import sse_frames

    async def still_connected() -> bool:
        return False

    frames = sse_frames(
        bus,
        workspace_id=chain.workspace.id,
        run_id=chain.run.id,
        last_seq=0,
        heartbeat_s=0.01,
        is_disconnected=still_connected,
        max_events=1,
    )
    frame = await frames.__anext__()
    assert "[REDACTED]" in frame
    assert "_truncated" in frame
    assert "id: " in frame
    assert long_value not in frame


async def test_bus_event_sink_publishes_with_fixed_scope(
    database: Database, session: AsyncSession
) -> None:
    chain = await make_chain(session, slug="sink")
    await session.commit()
    bus = EventBus(database.sessions)
    sink = BusEventSink(
        bus,
        workspace_id=chain.workspace.id,
        run_id=chain.run.id,
        agent_id=chain.agent.id,
    )

    async with bus.subscribe(chain.workspace.id, chain.run.id) as subscription:
        await sink.emit(EventType.AGENT_USAGE, {"tokens": 10})
        received = await subscription.__anext__()

    assert received.run_id == chain.run.id
    assert received.agent_id == chain.agent.id
    assert received.type is EventType.AGENT_USAGE


async def test_event_record_from_event_uses_payload_field(
    session: AsyncSession,
) -> None:
    chain = await make_chain(session, slug="record")
    row = await EventRepo(session, Event, chain.workspace.id).append(
        type=EventType.RUN_CREATED.value,
        payload_json={"hello": "world"},
        run_id=chain.run.id,
    )

    record = event_record_from_event(row)

    assert isinstance(record, EventRecord)
    assert record.payload == {"hello": "world"}
    assert record.type is EventType.RUN_CREATED
    assert record.seq == row.seq


async def test_event_bus_rejects_a_zero_queue(database: Database) -> None:
    with pytest.raises(ValueError, match="queue_size"):
        EventBus(database.sessions, queue_size=0)


async def test_subscription_aiter_returns_itself(database: Database) -> None:
    bus = EventBus(database.sessions)
    subscription = bus.subscribe("ws-1", None)

    assert subscription.__aiter__() is subscription


async def test_remove_subscriber_handles_an_unknown_workspace(database: Database) -> None:
    bus = EventBus(database.sessions)
    subscription = bus.subscribe("ws-1", None)

    bus._remove_subscriber(subscription)

    assert bus._subscribers == {}


async def test_remove_subscriber_keeps_the_workspace_entry_until_empty(
    database: Database,
) -> None:
    bus = EventBus(database.sessions)
    first = bus.subscribe("ws-1", None)
    second = bus.subscribe("ws-1", None)
    bus._add_subscriber(first)
    bus._add_subscriber(second)

    bus._remove_subscriber(first)

    assert set(bus._subscribers["ws-1"]) == {second}


async def test_put_live_dropping_a_gap_record_leaves_no_double_gap(
    database: Database,
) -> None:
    bus = EventBus(database.sessions, queue_size=1)
    subscription = bus.subscribe("ws-1", None)
    gap = EventRecord(
        seq=None,
        id=None,
        workspace_id="ws-1",
        run_id=None,
        agent_id=None,
        type=EventType.STREAM_GAP,
        payload={"missed_from": 7},
        ts=utcnow(),
    )
    subscription._queue.put_nowait(gap)
    live = EventRecord(
        seq=10,
        id="event-10",
        workspace_id="ws-1",
        run_id=None,
        agent_id=None,
        type=EventType.AGENT_MESSAGE,
        payload={},
        ts=utcnow(),
    )

    bus._put_live(subscription, live)

    assert subscription._gap_missed_from is None
    assert await subscription._queue.get() == live
