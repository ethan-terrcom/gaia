"""Tests for in-turn approval brokers (doc 10a requirement 8)."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from gaia.db.engine import session_scope
from gaia.db.models import Approval
from gaia.db.repo import ScopedRepo
from gaia.domain.enums import AgentRole, ApprovalKind, ApprovalStatus, EventType
from gaia.runtimes.approvals import (
    ApprovalDecision,
    ApprovalNotPending,
    DbApprovalBroker,
    InMemoryApprovalBroker,
    _decision_from_row,
    _idempotency_key,
)
from gaia.runtimes.base import AgentTask
from gaia.services.events import EventBus
from gaia.services.git import WorktreeLocation
from tests.conftest import Database
from tests.db_builders import Chain, make_chain
from tests.runtime_contract import wait_for_pending


def make_task(chain: Chain, tmp_path: Path) -> AgentTask:
    return AgentTask(
        task_id="task-approval",
        run_id=chain.run.id,
        run_step_id=chain.step.id,
        workspace_id=chain.workspace.id,
        agent_id=chain.agent.id,
        role=AgentRole.JUNIOR,
        instructions="run pytest",
        worktree=WorktreeLocation(work_tree=tmp_path, git_dir=tmp_path / ".git"),
        base_sha="0" * 40,
    )


async def test_in_memory_broker_waits_for_a_decision() -> None:
    broker = InMemoryApprovalBroker()
    task = _task_without_worktree("task-1", "ws-1", "run-1")

    request = asyncio.create_task(
        broker.request(
            task,
            kind=ApprovalKind.COMMAND,
            payload={"argv": ["pytest"]},
            timeout_s=5,
        )
    )
    approval_id = await wait_for_pending(broker)
    broker.decide(approval_id, True, "approved")
    decision = await asyncio.wait_for(request, timeout=5)

    assert decision == ApprovalDecision(True, "approved")
    assert broker.pending() == ()


async def test_in_memory_broker_denies_on_timeout() -> None:
    broker = InMemoryApprovalBroker()
    task = _task_without_worktree("task-2", "ws-1", "run-1")

    decision = await broker.request(
        task,
        kind=ApprovalKind.COMMAND,
        payload={"argv": ["pytest"]},
        timeout_s=0.05,
    )

    assert decision == ApprovalDecision(False, "timed out")
    assert broker.pending() == ()


async def test_in_memory_broker_rejects_unknown_decision() -> None:
    broker = InMemoryApprovalBroker()

    with pytest.raises(KeyError, match="no pending approval"):
        broker.decide("missing", True, "no")


async def test_db_broker_request_approve_publishes_and_wakes(
    database: Database,
    session: AsyncSession,
    tmp_path: Path,
) -> None:
    chain = await make_chain(session, slug="approval-db")
    await session.commit()
    bus = EventBus(database.sessions)
    broker = DbApprovalBroker(database.sessions, bus)
    task = make_task(chain, tmp_path)

    async with bus.subscribe(chain.workspace.id, chain.run.id) as subscription:
        request = asyncio.create_task(
            broker.request(
                task,
                kind=ApprovalKind.COMMAND,
                payload={"argv": ["pytest", "-q"]},
                timeout_s=5,
            )
        )
        requested = await asyncio.wait_for(subscription.__anext__(), timeout=5)
        assert requested.type is EventType.APPROVAL_REQUESTED
        approval_id = requested.payload["approval_id"]
        await broker.decide(chain.workspace.id, approval_id, True, "ok", chain.user.id)
        decision = await asyncio.wait_for(request, timeout=5)

    assert decision == ApprovalDecision(True, "ok")
    async with database.sessions() as check_session:
        row = await ScopedRepo(check_session, Approval, chain.workspace.id).get(approval_id)
    assert row is not None
    assert row.status == ApprovalStatus.APPROVED.value
    assert row.decided_by == chain.user.id
    assert row.comment == "ok"


async def test_db_broker_timeout_marks_the_row_expired(
    database: Database,
    session: AsyncSession,
    tmp_path: Path,
) -> None:
    chain = await make_chain(session, slug="approval-timeout")
    await session.commit()
    bus = EventBus(database.sessions)
    broker = DbApprovalBroker(database.sessions, bus)
    task = make_task(chain, tmp_path)

    decision = await broker.request(
        task,
        kind=ApprovalKind.COMMAND,
        payload={"argv": ["pytest"]},
        timeout_s=0.05,
    )

    assert decision == ApprovalDecision(False, "timed out")
    async with database.sessions() as check_session:
        rows = await ScopedRepo(check_session, Approval, chain.workspace.id).list(
            run_id=chain.run.id
        )
    assert len(rows) == 1
    assert rows[0].status == ApprovalStatus.EXPIRED.value


async def test_db_broker_upsert_is_idempotent(
    database: Database,
    session: AsyncSession,
    tmp_path: Path,
) -> None:
    chain = await make_chain(session, slug="approval-upsert")
    await session.commit()
    bus = EventBus(database.sessions)
    broker = DbApprovalBroker(database.sessions, bus)
    task = make_task(chain, tmp_path)
    key = _idempotency_key(task, {"argv": ["pytest"]})

    first, first_decided = await broker._upsert(
        task, ApprovalKind.COMMAND, {"argv": ["pytest"]}, key
    )
    second, second_decided = await broker._upsert(
        task, ApprovalKind.COMMAND, {"argv": ["pytest"]}, key
    )

    assert first == second
    assert first_decided is None
    assert second_decided is None
    async with database.sessions() as check_session:
        row = await ScopedRepo(check_session, Approval, chain.workspace.id).get(first)
    assert row is not None
    assert row.status == ApprovalStatus.PENDING.value
    assert row.idempotency_key == key


async def test_db_broker_decide_unknown_approval_raises(
    database: Database,
    session: AsyncSession,
) -> None:
    chain = await make_chain(session, slug="approval-missing")
    await session.commit()
    broker = DbApprovalBroker(database.sessions, EventBus(database.sessions))

    with pytest.raises(LookupError, match="no approval"):
        await broker.decide(chain.workspace.id, "missing", True, "no", chain.user.id)


async def test_db_broker_decide_without_waiter_updates_row(
    database: Database,
    session: AsyncSession,
    tmp_path: Path,
) -> None:
    chain = await make_chain(session, slug="approval-no-waiter")
    await session.commit()
    bus = EventBus(database.sessions)
    broker = DbApprovalBroker(database.sessions, bus)
    async with session_scope(database.sessions) as insert_session:
        repo = ScopedRepo(insert_session, Approval, chain.workspace.id)
        row = await repo.create(
            run_id=chain.run.id,
            idempotency_key="cmd:no-waiter",
            kind=ApprovalKind.COMMAND.value,
            payload_json={"argv": ["pytest"]},
        )

    await broker.decide(chain.workspace.id, row.id, False, "late", chain.user.id)

    async with database.sessions() as check_session:
        stored = await ScopedRepo(check_session, Approval, chain.workspace.id).get(row.id)
    assert stored is not None
    assert stored.status == ApprovalStatus.REJECTED.value
    assert stored.comment == "late"


async def test_db_broker_mark_expired_updates_a_pending_row(
    database: Database,
    session: AsyncSession,
    tmp_path: Path,
) -> None:
    chain = await make_chain(session, slug="approval-expired-pending")
    await session.commit()
    broker = DbApprovalBroker(database.sessions, EventBus(database.sessions))
    async with session_scope(database.sessions) as insert_session:
        repo = ScopedRepo(insert_session, Approval, chain.workspace.id)
        row = await repo.create(
            run_id=chain.run.id,
            idempotency_key="cmd:pending",
            kind=ApprovalKind.COMMAND.value,
            payload_json={"argv": ["pytest"]},
        )

    await broker._mark_expired(chain.workspace.id, row.id)

    async with database.sessions() as check_session:
        stored = await ScopedRepo(check_session, Approval, chain.workspace.id).get(row.id)
    assert stored is not None
    assert stored.status == ApprovalStatus.EXPIRED.value


async def test_db_broker_mark_expired_leaves_missing_and_decided_rows_alone(
    database: Database,
    session: AsyncSession,
    tmp_path: Path,
) -> None:
    chain = await make_chain(session, slug="approval-expired-noop")
    await session.commit()
    broker = DbApprovalBroker(database.sessions, EventBus(database.sessions))

    await broker._mark_expired(chain.workspace.id, "missing")
    async with session_scope(database.sessions) as insert_session:
        repo = ScopedRepo(insert_session, Approval, chain.workspace.id)
        row = await repo.create(
            run_id=chain.run.id,
            idempotency_key="cmd:already-decided",
            kind=ApprovalKind.COMMAND.value,
            payload_json={"argv": ["pytest"]},
            status=ApprovalStatus.APPROVED.value,
        )
    await broker._mark_expired(chain.workspace.id, row.id)

    async with database.sessions() as check_session:
        stored = await ScopedRepo(check_session, Approval, chain.workspace.id).get(row.id)
    assert stored is not None
    assert stored.status == ApprovalStatus.APPROVED.value


def test_decision_from_row_maps_missing_to_deny() -> None:
    assert _decision_from_row(None) == ApprovalDecision(False, "approval not found")


def test_decision_from_row_maps_rejected_to_deny() -> None:
    class Rejected:
        status = ApprovalStatus.REJECTED.value
        comment = "no"

    assert _decision_from_row(Rejected()) == ApprovalDecision(False, "no")  # type: ignore[arg-type]


def _task_without_worktree(task_id: str, workspace_id: str, run_id: str) -> AgentTask:
    return AgentTask(
        task_id=task_id,
        run_id=run_id,
        run_step_id=None,
        workspace_id=workspace_id,
        agent_id=None,
        role=AgentRole.JUNIOR,
        instructions="hello",
        worktree=WorktreeLocation(work_tree=Path("/tmp"), git_dir=Path("/tmp/.git")),
        base_sha="0" * 40,
    )


async def _decide_when_requested(broker: DbApprovalBroker, chain: Chain, *, approved: bool) -> None:
    """Answer the first pending request as soon as it is registered."""
    for _ in range(200):
        if broker._waiters:
            approval_id = next(iter(broker._waiters))
            await broker.decide(chain.workspace.id, approval_id, approved, "ok", chain.user.id)
            return
        await asyncio.sleep(0.005)
    raise AssertionError("no request was registered")


async def test_db_broker_a_decision_during_publish_is_not_lost(
    database: Database,
    session: AsyncSession,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Lead review 10a (D1): the waiter exists before approval.requested goes out."""
    chain = await make_chain(session, slug="approval-race")
    await session.commit()
    bus = EventBus(database.sessions)
    broker = DbApprovalBroker(database.sessions, bus)
    original_publish = bus.publish

    async def publish_then_decide(**kwargs: Any) -> Any:
        record = await original_publish(**kwargs)
        if kwargs["type"] is EventType.APPROVAL_REQUESTED:
            await broker.decide(
                chain.workspace.id, kwargs["payload"]["approval_id"], True, "fast", chain.user.id
            )
        return record

    monkeypatch.setattr(bus, "publish", publish_then_decide)

    decision = await broker.request(
        make_task(chain, tmp_path), kind=ApprovalKind.COMMAND, payload={"argv": ["x"]}, timeout_s=2
    )

    assert decision == ApprovalDecision(True, "fast")


async def test_db_broker_refuses_a_decision_on_an_expired_row(
    database: Database, session: AsyncSession, tmp_path: Path
) -> None:
    """Lead review 10a (D2): a late click must not rewrite an expired approval."""
    chain = await make_chain(session, slug="approval-late")
    await session.commit()
    broker = DbApprovalBroker(database.sessions, EventBus(database.sessions))
    task = make_task(chain, tmp_path)
    await broker.request(task, kind=ApprovalKind.COMMAND, payload={"argv": ["x"]}, timeout_s=0.01)
    async with database.sessions() as check:
        row = (await ScopedRepo(check, Approval, chain.workspace.id).list(run_id=chain.run.id))[0]

    with pytest.raises(ApprovalNotPending, match="expired"):
        await broker.decide(chain.workspace.id, row.id, True, "too late", chain.user.id)


async def test_db_broker_answers_a_repeated_request_from_the_row(
    database: Database, session: AsyncSession, tmp_path: Path
) -> None:
    """Lead review 10a (D3): a rejected command stays rejected within the task."""
    chain = await make_chain(session, slug="approval-repeat")
    await session.commit()
    broker = DbApprovalBroker(database.sessions, EventBus(database.sessions))
    task = make_task(chain, tmp_path)
    payload = {"argv": ["npm", "install"]}

    first = await asyncio.gather(
        broker.request(task, kind=ApprovalKind.COMMAND, payload=payload, timeout_s=2),
        _decide_when_requested(broker, chain, approved=False),
    )
    second = await broker.request(task, kind=ApprovalKind.COMMAND, payload=payload, timeout_s=0.01)

    assert first[0].approved is False
    assert second == ApprovalDecision(False, "ok")
    assert not broker._waiters


async def test_db_broker_asks_again_after_expiry(
    database: Database, session: AsyncSession, tmp_path: Path
) -> None:
    chain = await make_chain(session, slug="approval-reask")
    await session.commit()
    broker = DbApprovalBroker(database.sessions, EventBus(database.sessions))
    task = make_task(chain, tmp_path)
    payload = {"argv": ["npm", "install"]}
    await broker.request(task, kind=ApprovalKind.COMMAND, payload=payload, timeout_s=0.01)

    again = await asyncio.gather(
        broker.request(task, kind=ApprovalKind.COMMAND, payload=payload, timeout_s=2),
        _decide_when_requested(broker, chain, approved=True),
    )

    assert again[0].approved is True
