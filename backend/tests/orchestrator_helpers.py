"""Shared helpers for the 12a orchestrator graph tests."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from gaia.config.settings import Settings
from gaia.db.models import Repository, Ticket, User, Workspace, Worktree
from gaia.db.repo import ScopedRepo
from gaia.domain.enums import WorktreeStatus
from gaia.orchestrator.deps import GraphDeps
from gaia.services.events import EventBus
from tests.orchestrator_fakes import (
    FakeCheckRunner,
    FakeCommitter,
    FakePlanner,
    FakePullRequests,
    FakeReviewer,
    FakeStepRunner,
    FakeWorktreePreparer,
)


@dataclass(frozen=True)
class TicketParents:
    """The rows needed to start a run for one ticket."""

    workspace: Workspace
    user: User
    repository: Repository
    ticket: Ticket
    worktree_id: str


async def make_ticket_parents(session: AsyncSession, slug: str = "local") -> TicketParents:
    """Create a workspace, owner, repository and ready ticket, without a run."""
    workspace = Workspace(name=f"{slug} workspace", slug=slug)
    session.add(workspace)
    await session.flush()

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

    tickets = ScopedRepo(session, Ticket, workspace.id)
    ticket = await tickets.create(
        repo_id=repository.id,
        title=f"Ship {slug}",
        body="A body",
        priority=2,
        external_ref=f"{slug}-1",
        created_by=user.id,
    )

    # The fake WorktreePreparer returns this id, so the FK on run.worktree_id
    # is satisfied in tests.
    worktrees = ScopedRepo(session, Worktree, workspace.id)
    worktree = await worktrees.create(
        id="worktree-1",
        repo_id=repository.id,
        path=f"/tmp/{slug}/worktree",
        branch=f"gaia/{slug}",
        base_sha="0" * 40,
        status=WorktreeStatus.READY.value,
    )
    return TicketParents(
        workspace=workspace,
        user=user,
        repository=repository,
        ticket=ticket,
        worktree_id=worktree.id,
    )


@dataclass(frozen=True)
class Fakes:
    """All 12a interface fakes in one place."""

    worktrees: FakeWorktreePreparer
    planner: FakePlanner
    steps: FakeStepRunner
    checks: FakeCheckRunner
    reviewer: FakeReviewer
    committer: FakeCommitter
    pull_requests: FakePullRequests


def make_graph_deps(database: Any, settings: Settings, fakes: Fakes) -> GraphDeps:
    """Build graph dependencies on the test database and fakes."""
    return GraphDeps(
        worktrees=fakes.worktrees,
        planner=fakes.planner,
        steps=fakes.steps,
        checks=fakes.checks,
        reviewer=fakes.reviewer,
        committer=fakes.committer,
        pull_requests=fakes.pull_requests,
        sessions=database.sessions,
        events=EventBus(database.sessions),
        settings=settings,
    )


def make_fakes(
    *,
    plan=None,
    step_runner: FakeStepRunner | None = None,
    reviewer: FakeReviewer | None = None,
    pull_requests: FakePullRequests | None = None,
) -> Fakes:
    """Create a standard set of fakes, allowing a test to override one."""
    return Fakes(
        worktrees=FakeWorktreePreparer(),
        planner=FakePlanner(plan=plan),
        steps=step_runner or FakeStepRunner(),
        checks=FakeCheckRunner(),
        reviewer=reviewer or FakeReviewer(),
        committer=FakeCommitter(),
        pull_requests=pull_requests or FakePullRequests(),
    )
