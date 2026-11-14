"""Ticket service unit tests for the branches the API hides."""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from gaia.domain.enums import TicketStatus
from gaia.domain.state import IllegalTransition
from gaia.services.tickets import TicketNotFound, TicketParentNotFound, TicketService
from tests.db_builders import make_chain


async def test_create_requires_a_parent_in_the_same_workspace(
    session: AsyncSession,
) -> None:
    chain = await make_chain(session, slug="ticket-service-create")
    service = TicketService(session, chain.workspace.id)

    ticket = await service.create(repo_id=chain.repository.id, title="A ticket")
    assert ticket.repo_id == chain.repository.id

    with pytest.raises(TicketParentNotFound):
        await service.create(repo_id="foreign-repo-id", title="Orphan")


async def test_get_missing_ticket_raises(session: AsyncSession) -> None:
    chain = await make_chain(session, slug="ticket-service-get")
    service = TicketService(session, chain.workspace.id)

    with pytest.raises(TicketNotFound):
        await service.get("missing-ticket-id")


async def test_update_applies_fields_and_transition(session: AsyncSession) -> None:
    chain = await make_chain(session, slug="ticket-service-update")
    service = TicketService(session, chain.workspace.id)

    ticket = await service.update(
        chain.ticket.id,
        title="A better title",
        priority=4,
        status=TicketStatus.READY,
    )

    assert ticket.title == "A better title"
    assert ticket.priority == 4
    assert ticket.status == TicketStatus.READY


async def test_update_status_only_uses_the_transition_machine(session: AsyncSession) -> None:
    chain = await make_chain(session, slug="ticket-service-status-only")
    service = TicketService(session, chain.workspace.id)

    ticket = await service.update(chain.ticket.id, status=TicketStatus.READY)

    assert ticket.status == TicketStatus.READY
    assert ticket.title == chain.ticket.title


async def test_update_with_illegal_transition_raises(session: AsyncSession) -> None:
    chain = await make_chain(session, slug="ticket-service-illegal")
    service = TicketService(session, chain.workspace.id)

    with pytest.raises(IllegalTransition):
        await service.update(chain.ticket.id, status=TicketStatus.DONE)


async def test_list_page_returns_cursor_only_when_more_pages_exist(
    session: AsyncSession,
) -> None:
    chain = await make_chain(session, slug="ticket-service-page")
    service = TicketService(session, chain.workspace.id)
    for index in range(3):
        await service.create(repo_id=chain.repository.id, title=f"Ticket {index}")

    first = await service.list_page(limit=2)
    assert len(first.items) == 2
    assert first.next_cursor == first.items[-1].id

    second = await service.list_page(limit=2, after_id=first.next_cursor)
    assert len(second.items) == 2
    assert second.next_cursor is None
