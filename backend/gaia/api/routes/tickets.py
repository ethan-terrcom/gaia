"""Ticket CRUD and the dashboard's paginated board query (doc 06)."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, status

from gaia.api.deps import PrincipalDep, ScopedDep, append_audit_log
from gaia.domain.enums import TicketStatus
from gaia.domain.schemas import TicketCreate, TicketPage, TicketPatch, TicketRead
from gaia.services.tickets import TicketNotFound, TicketParentNotFound, TicketService

router = APIRouter(tags=["tickets"])


@router.post(
    "/tickets",
    response_model=TicketRead,
    status_code=status.HTTP_201_CREATED,
    summary="Create a ticket",
)
async def create_ticket(
    payload: TicketCreate,
    scoped: ScopedDep,
    principal: PrincipalDep,
) -> TicketRead:
    """Create a ticket in ``repo_id`` after proving the repo is in this workspace."""
    service = TicketService(scoped.session, scoped.workspace_id)
    try:
        ticket = await service.create(
            repo_id=payload.repo_id,
            title=payload.title,
            body=payload.body,
            priority=payload.priority,
            external_ref=payload.external_ref,
            created_by=principal.user_id,
        )
    except TicketParentNotFound:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "repository not found") from None
    await append_audit_log(
        scoped.session,
        scoped.workspace_id,
        actor_user_id=principal.user_id,
        action="ticket.create",
        target=ticket.id,
        details={"repo_id": ticket.repo_id},
    )
    return TicketRead.model_validate(ticket)


@router.get("/tickets", response_model=TicketPage, summary="List tickets")
async def list_tickets(
    scoped: ScopedDep,
    status_filter: Annotated[TicketStatus | None, Query(alias="status")] = None,
    repo_id: str | None = None,
    q: Annotated[str | None, Query(max_length=200)] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    cursor: Annotated[str | None, Query(max_length=36)] = None,
) -> TicketPage:
    """Return one page of tickets, filtered by status, repository and title."""
    service = TicketService(scoped.session, scoped.workspace_id)
    page = await service.list_page(
        status=status_filter.value if status_filter is not None else None,
        repo_id=repo_id,
        q=q,
        after_id=cursor,
        limit=limit,
    )
    return TicketPage(
        items=[TicketRead.model_validate(ticket) for ticket in page.items],
        next_cursor=page.next_cursor,
    )


@router.get("/tickets/{ticket_id}", response_model=TicketRead, summary="Get a ticket")
async def get_ticket(ticket_id: str, scoped: ScopedDep) -> TicketRead:
    """Return one ticket, or 404 - the same answer for another workspace."""
    service = TicketService(scoped.session, scoped.workspace_id)
    try:
        ticket = await service.get(ticket_id)
    except TicketNotFound:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "ticket not found") from None
    return TicketRead.model_validate(ticket)


@router.patch("/tickets/{ticket_id}", response_model=TicketRead, summary="Update a ticket")
async def update_ticket(
    ticket_id: str,
    payload: TicketPatch,
    scoped: ScopedDep,
    principal: PrincipalDep,
) -> TicketRead:
    """Update text fields and apply a legal status transition, if one is given."""
    service = TicketService(scoped.session, scoped.workspace_id)
    try:
        await service.get(ticket_id)
    except TicketNotFound:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "ticket not found") from None

    updates = payload.model_dump(exclude_unset=True)
    status_value = updates.pop("status", None)
    values = {key: value for key, value in updates.items() if value is not None}

    ticket = await service.update(ticket_id, status=status_value, **values)
    details: dict[str, object] = {"fields": sorted(values)}
    if status_value is not None:
        details["status"] = str(status_value)
    await append_audit_log(
        scoped.session,
        scoped.workspace_id,
        actor_user_id=principal.user_id,
        action="ticket.update",
        target=ticket.id,
        details=details,
    )
    return TicketRead.model_validate(ticket)
