"""Tenant isolation (doc 03 task 5).

The rule that matters: a repository constructed for workspace A can never see,
create or modify a row of workspace B, and it does not leak whether such a row
exists. Each test builds two workspaces, because a bug that only shows with one
tenant is exactly the bug this layer exists to prevent.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from gaia.db.models import AuditLog, Event, Ticket, Worktree
from gaia.db.repo import (
    AppendOnlyRepo,
    AuditLogRepo,
    EventRepo,
    NotFound,
    ScopedRepo,
    TicketRepo,
    WorkspaceMismatch,
)
from tests.db_builders import Chain, make_chain


@pytest.fixture
async def two_chains(session: AsyncSession) -> tuple[Chain, Chain]:
    """Two independent workspaces with a full chain each."""
    return await make_chain(session, slug="alpha"), await make_chain(session, slug="beta")


async def test_list_returns_only_the_repos_workspace(
    session: AsyncSession, two_chains: tuple[Chain, Chain]
) -> None:
    """A list never mixes tenants, however many rows the other one has."""
    alpha, beta = two_chains
    tickets = ScopedRepo(session, Ticket, alpha.workspace.id)
    rows = await tickets.list()
    assert [row.id for row in rows] == [alpha.ticket.id]

    beta_tickets = ScopedRepo(session, Ticket, beta.workspace.id)
    assert [row.id for row in await beta_tickets.list()] == [beta.ticket.id]


async def test_get_hides_another_workspaces_row(
    session: AsyncSession, two_chains: tuple[Chain, Chain]
) -> None:
    """``get`` answers ``None`` for a row that exists but belongs to someone else."""
    alpha, beta = two_chains
    tickets = ScopedRepo(session, Ticket, alpha.workspace.id)
    assert await tickets.get(beta.ticket.id) is None
    assert await tickets.get("nope") is None
    fetched = await tickets.get(alpha.ticket.id)
    assert fetched is not None
    assert fetched.title == alpha.ticket.title


async def test_create_puts_the_row_in_the_repos_workspace(session: AsyncSession) -> None:
    """The workspace is injected, so a caller cannot forget it."""
    chain = await make_chain(session, slug="sole")
    worktrees = ScopedRepo(session, Worktree, chain.workspace.id)
    worktree = await worktrees.create(repo_id=chain.repository.id, path="/tmp/x", branch="gaia/x")
    assert worktree.workspace_id == chain.workspace.id

    # Repeating the repo's own workspace is allowed; it is not a contradiction.
    again = await worktrees.create(
        repo_id=chain.repository.id,
        path="/tmp/y",
        branch="gaia/y",
        workspace_id=chain.workspace.id,
    )
    assert again.workspace_id == chain.workspace.id


async def test_create_refuses_another_workspace(
    session: AsyncSession, two_chains: tuple[Chain, Chain]
) -> None:
    """Writing into a foreign workspace is a bug, so it raises instead of inserting."""
    alpha, beta = two_chains
    worktrees = ScopedRepo(session, Worktree, alpha.workspace.id)
    with pytest.raises(WorkspaceMismatch, match="cannot write Worktree"):
        await worktrees.create(
            repo_id=alpha.repository.id,
            path="/tmp/z",
            branch="gaia/z",
            workspace_id=beta.workspace.id,
        )


async def test_update_changes_only_the_given_columns(session: AsyncSession) -> None:
    """An update is a set of column assignments, and untouched columns stay put."""
    chain = await make_chain(session, slug="update")
    tickets = ScopedRepo(session, Ticket, chain.workspace.id)
    updated = await tickets.update(chain.ticket.id, title="A better title", priority=5)
    assert updated.title == "A better title"
    assert updated.priority == 5
    assert updated.status == chain.ticket.status
    assert updated.body == chain.ticket.body


async def test_update_of_a_foreign_row_raises_not_found(
    session: AsyncSession, two_chains: tuple[Chain, Chain]
) -> None:
    """A foreign row is reported the same way as a missing one."""
    alpha, beta = two_chains
    tickets = ScopedRepo(session, Ticket, alpha.workspace.id)
    with pytest.raises(NotFound, match=beta.ticket.id):
        await tickets.update(beta.ticket.id, title="Hijacked")


async def test_update_refuses_to_move_a_row_between_workspaces(
    session: AsyncSession, two_chains: tuple[Chain, Chain]
) -> None:
    """``workspace_id`` is not a writable column for a scoped repo."""
    alpha, beta = two_chains
    tickets = ScopedRepo(session, Ticket, alpha.workspace.id)
    with pytest.raises(WorkspaceMismatch, match="cannot write Ticket"):
        await tickets.update(alpha.ticket.id, workspace_id=beta.workspace.id)


async def test_list_can_filter_page_and_order(session: AsyncSession) -> None:
    """Filters are column equalities, and paging is a limit plus an offset."""
    chain = await make_chain(session, slug="paging")
    tickets = ScopedRepo(session, Ticket, chain.workspace.id)
    for index in range(3):
        await tickets.create(repo_id=chain.repository.id, title=f"ticket {index}")

    backlog = await tickets.list(status="backlog")
    assert len(backlog) == 4  # the chain's ticket plus three more

    # Ordered by id, which is UUIDv7: creation order. The chain's ticket is first.
    page = await tickets.list(status="backlog", limit=2, offset=2)
    assert sorted(row.title for row in backlog) == [
        "Ship paging",
        "ticket 0",
        "ticket 1",
        "ticket 2",
    ]
    assert len(page) == 2
    assert backlog[:2] + page == backlog


async def test_list_after_id_uses_uuidv7_cursor_order(session: AsyncSession) -> None:
    """``after_id`` returns only rows that sort after the cursor."""
    chain = await make_chain(session, slug="cursor")
    tickets = ScopedRepo(session, Ticket, chain.workspace.id)
    for index in range(3):
        await tickets.create(repo_id=chain.repository.id, title=f"ticket {index}")

    first = await tickets.list(limit=1)
    after_first = await tickets.list(after_id=first[0].id)

    assert [row.id for row in after_first] == [row.id for row in await tickets.list()][1:]


async def test_delete_missing_row_raises_not_found(session: AsyncSession) -> None:
    """Delete is scoped, so a missing id is indistinguishable from a foreign one."""
    chain = await make_chain(session, slug="delete-missing")
    tickets = ScopedRepo(session, Ticket, chain.workspace.id)

    with pytest.raises(NotFound, match="missing-ticket"):
        await tickets.delete("missing-ticket")


async def test_delete_removes_the_row(session: AsyncSession) -> None:
    """A successful delete returns the row and removes it from later reads."""
    chain = await make_chain(session, slug="delete-row")
    tickets = ScopedRepo(session, Ticket, chain.workspace.id)

    deleted = await tickets.delete(chain.ticket.id)

    assert deleted.id == chain.ticket.id
    assert await tickets.get(chain.ticket.id) is None


async def test_ticket_search_supports_no_limit(session: AsyncSession) -> None:
    """The search method is also used without a limit, not only from the API."""
    chain = await make_chain(session, slug="search-no-limit")
    tickets = TicketRepo(session, Ticket, chain.workspace.id)

    rows = await tickets.search()

    assert [row.id for row in rows] == [chain.ticket.id]


async def test_an_append_only_repo_has_no_update_or_delete() -> None:
    """``event`` and ``audit_log`` expose exactly two methods (doc 03 task 6)."""
    public = {
        name
        for name in dir(AppendOnlyRepo)
        if not name.startswith("_") and callable(getattr(AppendOnlyRepo, name))
    }
    assert public == {"append", "list"}


async def test_appending_events_keeps_them_per_workspace(
    session: AsyncSession, two_chains: tuple[Chain, Chain]
) -> None:
    """Another workspace's events never appear in the replay."""
    alpha, beta = two_chains
    alpha_events = EventRepo(session, Event, alpha.workspace.id)
    beta_events = EventRepo(session, Event, beta.workspace.id)
    mine = await alpha_events.append(type="run.started", run_id=alpha.run.id)
    await beta_events.append(type="run.started", run_id=beta.run.id)

    assert [event.id for event in await alpha_events.list()] == [mine.id]
    assert [event.id for event in await alpha_events.list_for_run(alpha.run.id)] == [mine.id]
    assert await alpha_events.list_for_run(beta.run.id) == []


async def test_replay_from_a_seq_cursor_and_limit(session: AsyncSession) -> None:
    """``after_seq`` is the ``Last-Event-ID`` cursor, and ``limit`` bounds a page."""
    chain = await make_chain(session, slug="replay")
    events = EventRepo(session, Event, chain.workspace.id)
    appended = [
        await events.append(type=f"step.event.{index}", run_id=chain.run.id) for index in range(3)
    ]

    after_first = await events.list_for_run(chain.run.id, after_seq=appended[0].seq)
    assert [event.type for event in after_first] == ["step.event.1", "step.event.2"]

    bounded = await events.list_for_run(chain.run.id, limit=2)
    assert [event.type for event in bounded] == ["step.event.0", "step.event.1"]

    bounded_list = await events.list(limit=1)
    assert [event.id for event in bounded_list] == [appended[0].id]


async def test_the_audit_trail_is_ordered_by_time(session: AsyncSession) -> None:
    """Audit rows have independent ``ts`` values, so time is the order."""
    chain = await make_chain(session, slug="audit")
    trail = AuditLogRepo(session, AuditLog, chain.workspace.id)
    now = datetime.now(UTC)
    later = await trail.append(actor="system", action="second", ts=now)
    earlier = await trail.append(actor="system", action="first", ts=now - timedelta(minutes=5))

    assert [entry.id for entry in await trail.list()] == [earlier.id, later.id]


async def test_event_replay_can_span_the_whole_workspace(session: AsyncSession) -> None:
    """The SSE reconnect path uses ``run_id=None``, so that cursor is tested too."""
    chain = await make_chain(session, slug="workspace-replay")
    events = EventRepo(session, Event, chain.workspace.id)
    first = await events.append(type="run.created", run_id=chain.run.id)
    second = await events.append(type="step.started", run_id=None)

    rows = await events.list_after(after_seq=0)

    assert [row.seq for row in rows] == [first.seq, second.seq]
