"""Model round-trips: create, read back, and see the defaults applied.

Doc 03's Tests section asks that "every model round-trips create and read", and
that timestamps are UTC and timezone-aware. Each test writes through the real
repositories, expires the identity map so the row comes back from SQLite, and
compares what came out with what went in.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from gaia.db.models import Approval, AuditLog, Event, LlmUsage, PullRequest, Ticket
from gaia.db.repo import AuditLogRepo, EventRepo, ScopedRepo
from gaia.domain.enums import (
    AgentStatus,
    ApprovalKind,
    ApprovalStatus,
    CiStatus,
    PullRequestState,
    RunStatus,
    RunStepStatus,
    TicketStatus,
    WorktreeStatus,
)
from tests.db_builders import make_chain


def _is_aware_utc(value: datetime) -> bool:
    """True for an aware datetime expressed in UTC."""
    return value.tzinfo is not None and value.utcoffset() == UTC.utcoffset(None)


async def test_the_chain_round_trips_with_its_defaults(session: AsyncSession) -> None:
    """Every parent model comes back with the defaults its column declares."""
    chain = await make_chain(session)
    session.expunge_all()

    tickets = ScopedRepo(session, Ticket, chain.workspace.id)
    ticket = await tickets.get(chain.ticket.id)
    assert ticket is not None
    assert ticket.status == TicketStatus.BACKLOG.value
    assert ticket.priority == 2
    assert ticket.external_ref == "local-1"
    assert ticket.created_by == chain.user.id
    assert _is_aware_utc(ticket.created_at)
    assert _is_aware_utc(ticket.updated_at)

    assert chain.run.status == RunStatus.PENDING.value
    assert chain.run.cost_usd == Decimal(0)
    assert chain.run.junior_runtime == "native_deepseek"
    assert chain.run.worktree_id == chain.worktree.id
    assert chain.worktree.status == WorktreeStatus.PREPARING.value
    assert chain.agent.status == AgentStatus.IDLE.value
    assert chain.step.status == RunStepStatus.PENDING.value
    assert chain.step.attempts == 0
    assert chain.step.spec_json == {"files": ["gaia/api/routes.py"]}


async def test_a_uuid_id_is_generated_and_unique(session: AsyncSession) -> None:
    """Ids are UUIDv7 strings created in Python, not by the database."""
    first = await make_chain(session, slug="one")
    second = await make_chain(session, slug="two")
    ids = {first.workspace.id, second.workspace.id, first.ticket.id, second.ticket.id}
    assert len(ids) == 4
    for value in ids:
        assert len(value) == 36
        assert value[14] == "7"  # the version nibble of a UUIDv7


async def test_an_approval_round_trips_and_keeps_its_payload(session: AsyncSession) -> None:
    """``payload_json`` survives as a dict, and a decision stores both fields."""
    chain = await make_chain(session)
    approvals = ScopedRepo(session, Approval, chain.workspace.id)
    approval = await approvals.create(
        run_id=chain.run.id,
        kind=ApprovalKind.MERGE.value,
        payload_json={"number": 6, "checks": ["lint", "test"]},
    )
    assert approval.status == ApprovalStatus.PENDING.value

    approval = await approvals.update(
        approval.id,
        status=ApprovalStatus.APPROVED.value,
        decided_by=chain.user.id,
        decided_at=datetime.now(UTC),
        comment="Ship it",
    )
    session.expunge_all()

    reloaded = await approvals.get(approval.id)
    assert reloaded is not None
    assert reloaded.kind == ApprovalKind.MERGE.value
    assert reloaded.payload_json == {"number": 6, "checks": ["lint", "test"]}
    assert reloaded.status == ApprovalStatus.APPROVED.value
    assert reloaded.comment == "Ship it"
    assert reloaded.decided_at is not None
    assert _is_aware_utc(reloaded.decided_at)


async def test_a_pull_request_round_trips(session: AsyncSession) -> None:
    """The PR row keeps its number, its URL and its CI rollup."""
    chain = await make_chain(session)
    pulls = ScopedRepo(session, PullRequest, chain.workspace.id)
    pull = await pulls.create(
        run_id=chain.run.id,
        number=6,
        url="https://example.invalid/org/repo/pull/6",
        ci_status=CiStatus.PASSED.value,
    )
    session.expunge_all()

    reloaded = await pulls.get(pull.id)
    assert reloaded is not None
    assert reloaded.state == PullRequestState.OPEN.value
    assert reloaded.ci_status == CiStatus.PASSED.value
    assert reloaded.url.endswith("/pull/6")


async def test_llm_usage_round_trips_money_as_a_decimal(session: AsyncSession) -> None:
    """Cost is ``Decimal`` to the micro-dollar, never a float."""
    chain = await make_chain(session)
    usage_repo = ScopedRepo(session, LlmUsage, chain.workspace.id)
    usage = await usage_repo.create(
        run_id=chain.run.id,
        run_step_id=chain.step.id,
        agent_id=chain.agent.id,
        provider="deepseek",
        model="deepseek-v4-pro",
        request_id="req_123",
        input_tokens=1200,
        output_tokens=300,
        cache_read_tokens=800,
        cache_creation_tokens=100,
        price_tier="off_peak",
        cost_usd=Decimal("0.001234"),
    )
    session.expunge_all()

    reloaded = await usage_repo.get(usage.id)
    assert reloaded is not None
    assert isinstance(reloaded.cost_usd, Decimal)
    assert reloaded.cost_usd == Decimal("0.001234")
    assert (reloaded.input_tokens, reloaded.output_tokens) == (1200, 300)
    assert (reloaded.cache_read_tokens, reloaded.cache_creation_tokens) == (800, 100)
    assert reloaded.price_tier == "off_peak"


async def test_events_get_a_growing_seq(session: AsyncSession) -> None:
    """``seq`` is assigned by the database and grows: it is what SSE replays on."""
    chain = await make_chain(session)
    events = EventRepo(session, Event, chain.workspace.id)
    first = await events.append(type="run.started", run_id=chain.run.id)
    second = await events.append(
        type="step.started", run_id=chain.run.id, agent_id=chain.agent.id, payload_json={"n": 1}
    )
    assert second.seq > first.seq
    assert first.id != second.id
    assert _is_aware_utc(first.ts)

    session.expunge_all()
    replayed = await events.list_for_run(chain.run.id)
    assert [event.type for event in replayed] == ["run.started", "step.started"]
    assert [event.seq for event in replayed] == [first.seq, second.seq]


async def test_an_audit_log_row_round_trips(session: AsyncSession) -> None:
    """The audit trail keeps its actor, its target and its details."""
    chain = await make_chain(session)
    trail = AuditLogRepo(session, AuditLog, chain.workspace.id)
    entry = await trail.append(
        actor=f"user:{chain.user.id}",
        action="ticket.created",
        target=chain.ticket.id,
        details_json={"title": chain.ticket.title},
    )
    session.expunge_all()

    entries = await trail.list()
    assert [row.id for row in entries] == [entry.id]
    assert entries[0].details_json == {"title": chain.ticket.title}
    assert _is_aware_utc(entries[0].ts)
