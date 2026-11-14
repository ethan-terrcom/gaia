"""Builders for the parent chain every database test needs.

Doc 03's models reference each other (a ticket needs a repo, a run needs a
ticket and a worktree), and SQLite enforces foreign keys now
(``PRAGMA foreign_keys=ON``), so a test cannot insert a child without its
parents. These helpers make one workspace and a full chain, and let a test make
a second workspace to prove that scoping really scopes.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from gaia.db.models import Agent, Repository, Run, RunStep, Ticket, User, Workspace, Worktree
from gaia.db.repo import ScopedRepo
from gaia.domain.enums import AgentRole, RuntimeKind


@dataclass(frozen=True)
class Chain:
    """One row of each parent model, all in the same workspace."""

    workspace: Workspace
    user: User
    repository: Repository
    agent: Agent
    ticket: Ticket
    worktree: Worktree
    run: Run
    step: RunStep


async def make_workspace(session: AsyncSession, slug: str = "local") -> Workspace:
    """Create a workspace with a distinct slug per test."""
    workspace = Workspace(name=f"{slug} workspace", slug=slug)
    session.add(workspace)
    await session.flush()
    return workspace


async def make_chain(session: AsyncSession, slug: str = "local") -> Chain:
    """Create a workspace and the rows a run needs, through the real repos."""
    workspace = await make_workspace(session, slug)
    users = ScopedRepo(session, User, workspace.id)
    user = await users.create(email=f"owner@{slug}.localhost", display_name="Owner")

    repositories = ScopedRepo(session, Repository, workspace.id)
    repository = await repositories.create(
        name=f"repo-{slug}",
        local_path=f"/tmp/{slug}/repo",
        remote_url=f"https://example.invalid/{slug}/repo.git",
        setup_command=["uv", "sync", "--locked"],
        test_command=["uv", "run", "pytest"],
        created_by=user.id,
    )

    agents = ScopedRepo(session, Agent, workspace.id)
    agent = await agents.create(
        role=AgentRole.JUNIOR,
        runtime=RuntimeKind.NATIVE_DEEPSEEK,
        model="deepseek-v4-pro",
        display_name=f"junior-{slug}",
    )

    tickets = ScopedRepo(session, Ticket, workspace.id)
    ticket = await tickets.create(
        repo_id=repository.id,
        title=f"Ship {slug}",
        body="A body",
        priority=2,
        external_ref=f"{slug}-1",
        created_by=user.id,
    )

    worktrees = ScopedRepo(session, Worktree, workspace.id)
    worktree = await worktrees.create(
        repo_id=repository.id,
        path=f"/tmp/{slug}/worktree",
        branch=f"gaia/{slug}",
        base_sha="0" * 40,
    )

    runs = ScopedRepo(session, Run, workspace.id)
    run = await runs.create(
        ticket_id=ticket.id,
        repo_id=repository.id,
        worktree_id=worktree.id,
        junior_runtime=RuntimeKind.NATIVE_DEEPSEEK.value,
        graph_thread_id=f"thread-{slug}",
        created_by=user.id,
    )

    steps = ScopedRepo(session, RunStep, workspace.id)
    step = await steps.create(
        run_id=run.id,
        step_index=0,
        title="Add the endpoint",
        spec_json={"files": ["gaia/api/routes.py"]},
        assigned_agent_id=agent.id,
    )
    return Chain(
        workspace=workspace,
        user=user,
        repository=repository,
        agent=agent,
        ticket=ticket,
        worktree=worktree,
        run=run,
        step=step,
    )
