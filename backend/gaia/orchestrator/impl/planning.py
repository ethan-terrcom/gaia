"""The real planner: lead planning through the gateway (12b-2)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from gaia.config.settings import Settings
from gaia.db.engine import session_scope
from gaia.db.models import Repository, Ticket, Worktree
from gaia.db.repo import RunRepo, ScopedRepo
from gaia.domain.plans import Plan, TicketInput
from gaia.orchestrator.impl.lead_llm import lead_llm
from gaia.orchestrator.lead import LeadEngineer
from gaia.orchestrator.repo_context import build_repo_context
from gaia.security.commands import CommandPolicy
from gaia.services.costs import PriceTable


class GatewayPlanner:
    """Produce a ticket plan with the lead's configured structured LLM."""

    def __init__(
        self,
        *,
        settings: Settings,
        sessions: async_sessionmaker[AsyncSession],
        price_table: PriceTable,
        http_client: Any | None = None,
    ) -> None:
        self._settings = settings
        self._sessions = sessions
        self._price_table = price_table
        self._http_client = http_client

    async def plan(self, run_id: str) -> Plan:
        """Plan ``run_id``'s ticket, holding no DB session during the LLM call."""
        async with session_scope(self._sessions) as session:
            run = await RunRepo(session).get_by_id(run_id)
            if run is None:
                raise ValueError(f"no run with id {run_id!r}")
            workspace_id = run.workspace_id
            ticket = await ScopedRepo(session, Ticket, workspace_id).get(run.ticket_id)
            if ticket is None:
                raise ValueError(f"run {run_id!r} has no ticket {run.ticket_id!r}")
            repository = await ScopedRepo(session, Repository, workspace_id).get(run.repo_id)
            if repository is None:
                raise ValueError(f"run {run_id!r} has no repository {run.repo_id!r}")
            if run.worktree_id is None:
                raise ValueError(f"run {run_id!r} has no worktree")
            worktree = await ScopedRepo(session, Worktree, workspace_id).get(run.worktree_id)
            if worktree is None:
                raise ValueError(f"run {run_id!r} has no worktree {run.worktree_id!r}")

            ticket_input = TicketInput(
                id=ticket.id,
                title=ticket.title,
                body=ticket.body or "",
            )
            worktree_root = Path(worktree.path)
            test_command = tuple(repository.test_command) if repository.test_command else ()

        repo_context = await build_repo_context(worktree_root, ticket_input)
        extra_allow = (test_command,) if test_command else ()

        async with lead_llm(
            settings=self._settings,
            sessions=self._sessions,
            price_table=self._price_table,
            run_id=run_id,
            workspace_id=workspace_id,
            http_client=self._http_client,
        ) as llm:
            engineer = LeadEngineer(llm, CommandPolicy.from_default())
            return await engineer.plan_ticket(
                ticket_input,
                repo_context,
                extra_allow=extra_allow,
            )
