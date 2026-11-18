"""End-to-end graph test with the real execution services (doc 12b-1).

The planner and reviewer stay faked (they are 12b-2); everything from worktree
preparation through implementation, checks and commits runs through the real
12b-1 implementations, with the runtimes faked but registered under both
:class:`RuntimeKind` values.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from gaia.config.settings import Settings
from gaia.db.engine import session_scope
from gaia.db.models import Repository, Run, RunStep, Ticket, User, Worktree
from gaia.db.repo import ScopedRepo
from gaia.domain.enums import RunStatus, RunStepStatus
from gaia.orchestrator.deps import GraphDeps
from gaia.orchestrator.graph import build_graph
from gaia.orchestrator.impl import worktree_location
from gaia.orchestrator.impl.checks import SandboxCheckRunner
from gaia.orchestrator.impl.commits import WorktreeCommitter
from gaia.orchestrator.impl.steps import RuntimeStepRunner
from gaia.orchestrator.impl.worktrees import WorktreeSetup
from gaia.orchestrator.runner import RunManager, open_checkpointer
from gaia.runtimes.base import AgentResult, AgentTask, RuntimeRegistry, RuntimeUsage
from gaia.security.commands import CommandPolicy
from gaia.services.costs import load_price_table
from gaia.services.events import EventBus
from gaia.services.git import GitService
from tests.db_builders import make_workspace
from tests.orchestrator_fakes import (
    FakePlanner,
    FakePullRequests,
    FakeReviewer,
    make_plan,
)
from tests.tool_fakes import FakeCommandRunner


class StepIndexedRuntime:
    """A fake runtime that writes one distinct file per ``run_step``."""

    def __init__(self, name: str) -> None:
        self.name = name

    async def run_task(self, task: AgentTask, events: Any) -> AgentResult:
        del events
        path = Path(task.worktree.work_tree) / f"step-{task.run_step_id}.txt"
        path.write_text(f"step {task.run_step_id}\n", encoding="utf-8")
        return AgentResult(
            status="completed",
            summary="done",
            structured_output=None,
            files_changed=(),
            usage=RuntimeUsage(),
            error=None,
            transcript_ref=None,
        )

    async def cancel(self, task_id: str) -> None:
        del task_id


async def _wait_for_task(manager: RunManager, run_id: str, *, timeout_s: float = 30.0) -> None:
    """Wait for the current graph task for ``run_id`` to finish."""
    async with asyncio.timeout(timeout_s):
        await manager._tasks[run_id]


async def test_real_execution_services_drive_the_graph_to_completion(
    database: Any,
    git_repo: Path,
    git_settings: Settings,
    git_service: GitService,
) -> None:
    settings = git_settings
    async with session_scope(database.sessions) as session:
        workspace = await make_workspace(session)
        workspace_id = workspace.id
        users = ScopedRepo(session, User, workspace_id)
        user = await users.create(email="owner@real.localhost", display_name="Owner")
        repositories = ScopedRepo(session, Repository, workspace_id)
        repository = await repositories.create(
            name="real-execution",
            local_path=str(git_repo),
            setup_command=None,
            test_command=["pytest", "-q"],
            created_by=user.id,
        )
        tickets = ScopedRepo(session, Ticket, workspace_id)
        ticket = await tickets.create(
            repo_id=repository.id,
            title="Ship the real execution path",
            body="A body",
            created_by=user.id,
        )

    planner = FakePlanner(plan=make_plan(2))
    reviewer = FakeReviewer()
    pull_requests = FakePullRequests()

    registry = RuntimeRegistry()
    registry.register(StepIndexedRuntime("native_deepseek"))
    registry.register(StepIndexedRuntime("claude_agent_sdk"))

    bus = EventBus(database.sessions)
    deps = GraphDeps(
        worktrees=WorktreeSetup(
            settings=settings,
            sessions=database.sessions,
            git=git_service,
            commands=FakeCommandRunner(),
        ),
        planner=planner,
        steps=RuntimeStepRunner(
            settings=settings,
            sessions=database.sessions,
            git=git_service,
            runtimes=registry,
            bus=bus,
            price_table=load_price_table(),
        ),
        checks=SandboxCheckRunner(
            settings=settings,
            sessions=database.sessions,
            commands=FakeCommandRunner(),
            policy=CommandPolicy.from_default(),
        ),
        reviewer=reviewer,
        committer=WorktreeCommitter(
            sessions=database.sessions,
            git=git_service,
        ),
        pull_requests=pull_requests,
        sessions=database.sessions,
        events=bus,
        settings=settings,
    )

    async with open_checkpointer(settings) as saver:
        graph = build_graph(deps, checkpointer=saver)
        manager = RunManager(graph, deps)
        try:
            run_id = await manager.start(
                ticket.id,
                workspace_id=workspace_id,
                created_by=user.id,
            )
            await _wait_for_task(manager, run_id)

            # Approve the plan.
            await manager.resume(run_id, {"action": "approve"})
            await _wait_for_task(manager, run_id)

            # Approve opening the PR.
            await manager.resume(run_id, {"action": "approve"})
            await _wait_for_task(manager, run_id)

            # Approve the merge.
            await manager.resume(run_id, {"action": "approve"})
            await _wait_for_task(manager, run_id)
        finally:
            await manager.aclose()

    async with session_scope(database.sessions) as session:
        run = await ScopedRepo(session, Run, workspace_id).get(run_id)
        assert run is not None
        assert run.status == RunStatus.COMPLETED.value
        step_rows = await ScopedRepo(session, RunStep, workspace_id).list(run_id=run_id)
        assert sorted(row.step_index for row in step_rows) == [0, 1]
        assert {row.status for row in step_rows} == {RunStepStatus.COMPLETED.value}
        assert all(
            row.result_summary is not None and row.result_summary.startswith("committed as")
            for row in step_rows
        )
        run_row = run
        worktree_id = run_row.worktree_id
        assert worktree_id is not None
        worktree_row = await ScopedRepo(session, Worktree, workspace_id).get(worktree_id)
        assert worktree_row is not None

    # Build the location after the DB session closes: this can run git.
    repo_path = await git_service.validate_repo(repository.local_path)
    location = await worktree_location(
        git=git_service,
        repo_path=repo_path,
        worktree=worktree_row,
    )
    commits = await git_service.log(location, limit=10)
    summaries = [commit.summary for commit in commits]
    assert "[gaia] Step 1" in summaries
    assert "[gaia] Step 2" in summaries

    # The lead-side fakes were still the ones driving plan/review/PR.
    assert planner.calls == [run_id]
    assert len(reviewer.review_step_calls) == 2
    assert len(reviewer.final_review_calls) == 1
    assert pull_requests.open_calls == [run_id]
    assert pull_requests.watch_calls == [(run_id, 42)]
    assert pull_requests.merge_calls == [(run_id, 42)]
