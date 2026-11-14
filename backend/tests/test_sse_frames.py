"""Direct tests for the SSE frame generator (doc 13)."""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from gaia.api.routes.stream import _sse_frame, sse_frames
from gaia.db.types import utcnow
from gaia.domain.enums import EventType
from gaia.services.events import EventBus, EventRecord
from tests.conftest import Database
from tests.db_builders import make_chain


async def _not_disconnected() -> bool:
    return False


async def _disconnected() -> bool:
    return True


async def test_sse_frames_replays_only_until_the_test_bound(
    database: Database, session: AsyncSession
) -> None:
    chain = await make_chain(session, slug="sse-bound")
    await session.commit()
    bus = EventBus(database.sessions)
    await bus.publish(
        workspace_id=chain.workspace.id,
        type=EventType.RUN_CREATED,
        payload={"index": 1},
        run_id=chain.run.id,
    )
    await bus.publish(
        workspace_id=chain.workspace.id,
        type=EventType.RUN_STATUS_CHANGED,
        payload={"index": 2},
        run_id=chain.run.id,
    )

    frames = sse_frames(
        bus,
        workspace_id=chain.workspace.id,
        run_id=chain.run.id,
        last_seq=0,
        heartbeat_s=0.01,
        is_disconnected=_not_disconnected,
        max_events=1,
    )

    assert "id: " in await frames.__anext__()
    with pytest.raises(StopAsyncIteration):
        await frames.__anext__()


async def test_sse_frames_yields_a_live_event(
    database: Database, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    chain = await make_chain(session, slug="sse-live")
    await session.commit()
    bus = EventBus(database.sessions)

    async def replay_then_publish(
        workspace_id: str,
        run_id: str | None,
        after_seq: int,
        limit: int | None,
    ) -> list[EventRecord]:
        await bus.publish(
            workspace_id=workspace_id,
            type=EventType.AGENT_MESSAGE,
            payload={"live": True},
            run_id=run_id,
        )
        return []

    monkeypatch.setattr(bus, "replay", replay_then_publish)
    frames = sse_frames(
        bus,
        workspace_id=chain.workspace.id,
        run_id=chain.run.id,
        last_seq=0,
        heartbeat_s=0.01,
        is_disconnected=_not_disconnected,
        max_events=1,
    )

    frame = await frames.__anext__()
    assert "id: " in frame
    assert "agent.message" in frame


async def test_sse_frames_heartbeats_when_idle(database: Database, session: AsyncSession) -> None:
    chain = await make_chain(session, slug="sse-heartbeat")
    await session.commit()
    bus = EventBus(database.sessions)

    frames = sse_frames(
        bus,
        workspace_id=chain.workspace.id,
        run_id=chain.run.id,
        last_seq=0,
        heartbeat_s=0.01,
        is_disconnected=_not_disconnected,
        max_events=1,
    )

    assert await frames.__anext__() == ": ping\n\n"
    # Resuming after the yield executes the `continue` that follows it.
    assert await frames.__anext__() == ": ping\n\n"
    await frames.aclose()


async def test_sse_frames_skips_a_duplicate_and_then_yields_new_live_events(
    database: Database, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    chain = await make_chain(session, slug="sse-duplicate")
    await session.commit()
    bus = EventBus(database.sessions)

    async def replay_with_live_duplicate(
        workspace_id: str,
        run_id: str | None,
        after_seq: int,
        limit: int | None,
    ) -> list[EventRecord]:
        first = await bus.publish(
            workspace_id=workspace_id,
            type=EventType.RUN_CREATED,
            payload={"index": 1},
            run_id=run_id,
        )
        await bus.publish(
            workspace_id=workspace_id,
            type=EventType.RUN_STATUS_CHANGED,
            payload={"index": 2},
            run_id=run_id,
        )
        return [first]

    monkeypatch.setattr(bus, "replay", replay_with_live_duplicate)
    frames = sse_frames(
        bus,
        workspace_id=chain.workspace.id,
        run_id=chain.run.id,
        last_seq=0,
        heartbeat_s=0.01,
        is_disconnected=_not_disconnected,
        max_events=2,
    )

    first = await frames.__anext__()
    second = await frames.__anext__()
    assert "run.created" in first
    assert "run.status_changed" in second
    # Resume after the second yield so the generator can run `emitted += 1`,
    # update `last_replayed_seq`, hit the max-events bound and return.
    with pytest.raises(StopAsyncIteration):
        await frames.__anext__()


async def test_sse_frames_heals_an_overflow_from_the_database(
    database: Database, session: AsyncSession
) -> None:
    """Lead fix (review 13, E2): a slow subscriber gets every event once, in order."""
    chain = await make_chain(session, slug="sse-heal-gap")
    await session.commit()
    bus = EventBus(database.sessions, queue_size=2)
    frames = sse_frames(
        bus,
        workspace_id=chain.workspace.id,
        run_id=chain.run.id,
        last_seq=None,
        heartbeat_s=0.01,
        is_disconnected=_not_disconnected,
        max_events=5,
    )
    assert await frames.__anext__() == ": ping\n\n"  # subscribed and idle

    published = [
        await bus.publish(
            workspace_id=chain.workspace.id,
            type=EventType.AGENT_MESSAGE,
            payload={"index": index},
            run_id=chain.run.id,
        )
        for index in range(5)
    ]  # the queue holds 2, so 3 are dropped and a gap is pending

    received = [await frames.__anext__() for _ in range(5)]

    assert [frame.splitlines()[0] for frame in received] == [
        f"id: {event.seq}" for event in published
    ]
    assert not any("stream.gap" in frame for frame in received)
    with pytest.raises(StopAsyncIteration):
        await frames.__anext__()


async def test_sse_frames_without_a_resume_point_is_live_only(
    database: Database, session: AsyncSession
) -> None:
    """Lead fix (review 13, E1): a new connection does not replay the whole log."""
    chain = await make_chain(session, slug="sse-live-only")
    await session.commit()
    bus = EventBus(database.sessions)
    await bus.publish(
        workspace_id=chain.workspace.id,
        type=EventType.RUN_CREATED,
        payload={},
        run_id=chain.run.id,
    )
    frames = sse_frames(
        bus,
        workspace_id=chain.workspace.id,
        run_id=chain.run.id,
        last_seq=None,
        heartbeat_s=0.01,
        is_disconnected=_not_disconnected,
        max_events=1,
    )

    assert await frames.__anext__() == ": ping\n\n"
    live = await bus.publish(
        workspace_id=chain.workspace.id,
        type=EventType.RUN_STATUS_CHANGED,
        payload={},
        run_id=chain.run.id,
    )
    assert (await frames.__anext__()).startswith(f"id: {live.seq}\n")


async def test_sse_frames_replays_in_pages(
    database: Database, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    chain = await make_chain(session, slug="sse-paged")
    await session.commit()
    bus = EventBus(database.sessions)
    for index in range(5):
        await bus.publish(
            workspace_id=chain.workspace.id,
            type=EventType.AGENT_MESSAGE,
            payload={"index": index},
            run_id=chain.run.id,
        )
    monkeypatch.setattr("gaia.api.routes.stream.REPLAY_PAGE_SIZE", 2)
    frames = sse_frames(
        bus,
        workspace_id=chain.workspace.id,
        run_id=chain.run.id,
        last_seq=0,
        heartbeat_s=0.01,
        is_disconnected=_not_disconnected,
        max_events=5,
    )

    received = [frame async for frame in frames]

    assert len(received) == 5
    assert ["index" in frame for frame in received] == [True] * 5


async def test_sse_frames_stops_when_the_client_disconnects(
    database: Database, session: AsyncSession
) -> None:
    chain = await make_chain(session, slug="sse-disconnect")
    await session.commit()
    bus = EventBus(database.sessions)

    frames = sse_frames(
        bus,
        workspace_id=chain.workspace.id,
        run_id=chain.run.id,
        last_seq=0,
        heartbeat_s=0.01,
        is_disconnected=_disconnected,
        max_events=1,
    )

    with pytest.raises(StopAsyncIteration):
        await frames.__anext__()


def test_sse_frame_for_a_gap_has_no_id_line() -> None:
    gap = EventRecord(
        seq=None,
        id=None,
        workspace_id="ws-1",
        run_id=None,
        agent_id=None,
        type=EventType.STREAM_GAP,
        payload={"missed_from": 3},
        ts=utcnow(),
    )

    frame = _sse_frame(gap)

    assert not frame.startswith("id:")
    assert "event: stream.gap" in frame
    assert '"missed_from":3' in frame or '"missed_from": 3' in frame


async def test_sse_frames_stops_at_the_bound_while_healing(
    database: Database, session: AsyncSession
) -> None:
    chain = await make_chain(session, slug="sse-heal-bound")
    await session.commit()
    bus = EventBus(database.sessions, queue_size=1)
    frames = sse_frames(
        bus,
        workspace_id=chain.workspace.id,
        run_id=chain.run.id,
        last_seq=None,
        heartbeat_s=0.01,
        is_disconnected=_not_disconnected,
        max_events=2,
    )
    assert await frames.__anext__() == ": ping\n\n"
    for index in range(4):
        await bus.publish(
            workspace_id=chain.workspace.id,
            type=EventType.AGENT_MESSAGE,
            payload={"index": index},
            run_id=chain.run.id,
        )

    assert len([await frames.__anext__() for _ in range(2)]) == 2
    with pytest.raises(StopAsyncIteration):
        await frames.__anext__()
