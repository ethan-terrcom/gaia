"""Ticket workflows for the repositories & tickets API (doc 06).

The route layer stays thin: this service owns the two rules that are easy to get
wrong when tickets are written from an HTTP body.

- **Parents are loaded in the caller's workspace.** A ``repo_id`` from a request
  body is an id, not a trust statement. The service fetches it through
  :class:`gaia.db.repo.ScopedRepo` before creating the child row, so a foreign
  repository id is indistinguishable from one that does not exist (review F5).
- **Status moves go through :func:`gaia.domain.state.transition`.** A PATCH may
  set text fields directly, but a status is a workflow move and must obey the
  legal transition table.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from gaia.db.models import Repository, Ticket
from gaia.db.repo import ScopedRepo, TicketRepo
from gaia.domain.state import transition


class TicketParentNotFound(LookupError):
    """The requested repository does not exist in this workspace."""

    def __init__(self, repo_id: str) -> None:
        self.repo_id = repo_id
        super().__init__(f"no repository with id {repo_id!r} in this workspace")


class TicketNotFound(LookupError):
    """The requested ticket does not exist in this workspace."""

    def __init__(self, ticket_id: str) -> None:
        self.ticket_id = ticket_id
        super().__init__(f"no ticket with id {ticket_id!r} in this workspace")


@dataclass(frozen=True)
class TicketPage:
    """One page of tickets and the cursor for the next page."""

    items: list[Ticket]
    next_cursor: str | None


class TicketService:
    """Create, read and update one workspace's tickets."""

    def __init__(self, session: AsyncSession, workspace_id: str) -> None:
        self.tickets: TicketRepo = TicketRepo(session, Ticket, workspace_id)
        self.repositories: ScopedRepo[Repository] = ScopedRepo(session, Repository, workspace_id)

    async def create(
        self,
        *,
        repo_id: str,
        title: str,
        body: str = "",
        priority: int = 0,
        external_ref: str | None = None,
        created_by: str | None = None,
    ) -> Ticket:
        """Create a ticket after proving ``repo_id`` belongs to this workspace."""
        repo = await self.repositories.get(repo_id)
        if repo is None:
            raise TicketParentNotFound(repo_id)
        return await self.tickets.create(
            repo_id=repo.id,
            title=title,
            body=body,
            priority=priority,
            external_ref=external_ref,
            created_by=created_by,
        )

    async def get(self, ticket_id: str) -> Ticket:
        """Return ``ticket_id``, or raise :class:`TicketNotFound`."""
        ticket = await self.tickets.get(ticket_id)
        if ticket is None:
            raise TicketNotFound(ticket_id)
        return ticket

    async def update(
        self,
        ticket_id: str,
        *,
        status: str | None = None,
        **values: Any,
    ) -> Ticket:
        """Update text fields, then apply a legal status transition if given."""
        ticket = await self.get(ticket_id)
        if status is not None:
            transition(ticket, status)
        if values:
            ticket = await self.tickets.update(ticket_id, **values)
        return ticket

    async def list_page(
        self,
        *,
        status: str | None = None,
        repo_id: str | None = None,
        q: str | None = None,
        after_id: str | None = None,
        limit: int,
    ) -> TicketPage:
        """Return one page of tickets and the cursor for the next page.

        Fetches ``limit + 1`` rows: the extra row is only used to decide whether
        ``next_cursor`` exists, and it is not returned. The cursor is the last
        returned row's id (UUIDv7, so creation order).
        """
        rows = await self.tickets.search(
            status=status,
            repo_id=repo_id,
            q=q,
            after_id=after_id,
            limit=limit + 1,
        )
        has_more = len(rows) > limit
        items = rows[:limit]
        next_cursor = items[-1].id if has_more and items else None
        return TicketPage(items=items, next_cursor=next_cursor)
