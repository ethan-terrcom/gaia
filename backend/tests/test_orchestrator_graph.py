"""End-to-end graph tests for doc 12a, using a real AsyncSqliteSaver."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from langchain_core.runnables import RunnableConfig
from langgraph.types import Command
from sqlalchemy.ext.asyncio import AsyncSession

from gaia.config.settings import Settings
from gaia.db.engine import session_scope
from gaia.db.models import Approval, Event, Run, RunStep, Ticket
from gaia.db.repo import EventRepo, ScopedRepo
from gaia.domain.enums import ApprovalKind, ApprovalStatus, EventType, RunStatus, TicketStatus
from gaia.orchestrator.deps import GraphDeps
from gaia.orchestrator.graph import build_graph
from gaia.orchestrator.runner import RunManager, open_checkpointer
from gaia.orchestrator.state import RunState
from gaia.services.events import EventBus
from tests.orchestrator_fakes import FakeReviewer, FakeStepRunner, make_plan, make_review
from tests.orchestrator_helpers import Fakes, make_fakes, make_ticket_parents


def make_deps(database: Any, settings: Settings, fakes: Fakes) -> GraphDeps:
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


def run_config(run_id: str) -> RunnableConfig:
    return {"configurable": {"thread_id": run_id}}


def initial_state(run_id: str, *, workspace_id: str, ticket_id: str, repo_id: str) -> RunState:
    return {
        "run_id": run_id,
        "ticket_id": ticket_id,
        "repo_id": repo_id,
        "workspace_id": workspace_id,
        "worktree_id": None,
        "plan": None,
        "step_index": 0,
        "attempt": 1,
        "feedback": None,
        "step_results": [],
        "review": None,
        "pr_number": None,
        "error": None,
        "route": None,
    }


@asynccontextmanager
async def graph_env(
    database: Any,
    tmp_path: Path,
    fakes: Fakes,
    *,
    max_concurrent_runs: int = 3,
) -> AsyncIterator[tuple[RunManager, GraphDeps, Settings]]:
    """Open a graph and manager with a real checkpointer on a temp file."""
    settings = Settings(
        data_dir=tmp_path / "gaia-data",
        checkpoint_path=tmp_path / "checkpoints.sqlite",
        max_concurrent_runs=max_concurrent_runs,
    )
    deps = make_deps(database, settings, fakes)
    async with open_checkpointer(settings) as saver:
        graph = build_graph(deps, checkpointer=saver)
        manager = RunManager(graph, deps, max_concurrent_runs=max_concurrent_runs)
        try:
            yield manager, deps, settings
        finally:
            await manager.aclose()


async def wait_for_task(manager: RunManager, run_id: str, *, timeout_s: float = 10.0) -> None:
    """Wait for the current graph task for ``run_id`` to finish."""
    async with asyncio.timeout(timeout_s):
        await manager._tasks[run_id]


async def wait_for_step_calls(
    step_runner: FakeStepRunner, count: int, *, timeout_s: float = 5.0
) -> None:
    """Wait until the fake step runner has recorded ``count`` calls."""
    async with asyncio.timeout(timeout_s):
        await step_runner.wait_for_calls(count)


async def _approval_count(session: AsyncSession, workspace_id: str, run_id: str, kind: str) -> int:
    approvals = ScopedRepo(session, Approval, workspace_id)
    rows = await approvals.list(run_id=run_id)
    return sum(1 for row in rows if row.kind == kind)


async def _run(session: AsyncSession, workspace_id: str, run_id: str) -> Run:
    run = await ScopedRepo(session, Run, workspace_id).get(run_id)
    assert run is not None
    return run


async def test_happy_path_completes_and_moves_ticket_to_done(database: Any, tmp_path: Path) -> None:
    fakes = make_fakes(plan=make_plan(2))
    async with session_scope(database.sessions) as session:
        parents = await make_ticket_parents(session)
        workspace_id = parents.workspace.id
        ticket_id = parents.ticket.id
        repo_id = parents.repository.id

    async with graph_env(database, tmp_path, fakes) as (manager, _deps, _settings):
        run_id = await manager.start(
            ticket_id, workspace_id=workspace_id, created_by=parents.user.id
        )
        await wait_for_task(manager, run_id)

        async with session_scope(database.sessions) as session:
            run = await _run(session, workspace_id, run_id)
            assert run.status == RunStatus.AWAITING_APPROVAL.value
            assert (
                await _approval_count(session, workspace_id, run_id, ApprovalKind.PLAN.value) == 1
            )

        await manager.resume(run_id, {"action": "approve"})
        await wait_for_task(manager, run_id)

        async with session_scope(database.sessions) as session:
            run = await _run(session, workspace_id, run_id)
            assert run.status == RunStatus.AWAITING_APPROVAL.value
            assert (
                await _approval_count(session, workspace_id, run_id, ApprovalKind.PR_OPEN.value)
                == 1
            )

        await manager.resume(run_id, {"action": "approve"})
        await wait_for_task(manager, run_id)

        async with session_scope(database.sessions) as session:
            run = await _run(session, workspace_id, run_id)
            assert run.status == RunStatus.AWAITING_APPROVAL.value
            assert (
                await _approval_count(session, workspace_id, run_id, ApprovalKind.MERGE.value) == 1
            )
            # Lead review 14 (M1): the merge approval carries the PR number the
            # human must type to confirm.
            [merge_row] = await ScopedRepo(session, Approval, workspace_id).list(
                run_id=run_id, kind=ApprovalKind.MERGE.value
            )
            assert merge_row.payload_json["pr_number"] == fakes.pull_requests.pr_number
            ticket = await ScopedRepo(session, Ticket, workspace_id).get(ticket_id)
            assert ticket is not None
            assert ticket.status == TicketStatus.IN_REVIEW.value

        await manager.resume(run_id, {"action": "approve"})
        await wait_for_task(manager, run_id)

        async with session_scope(database.sessions) as session:
            run = await _run(session, workspace_id, run_id)
            assert run.status == RunStatus.COMPLETED.value
            ticket = await ScopedRepo(session, Ticket, workspace_id).get(ticket_id)
            assert ticket is not None
            assert ticket.status == TicketStatus.DONE.value

    assert len(fakes.steps.calls) == 2
    assert len(fakes.committer.calls) == 2
    assert len(fakes.pull_requests.open_calls) == 1
    assert len(fakes.pull_requests.watch_calls) == 1
    assert len(fakes.pull_requests.merge_calls) == 1
    # repo_id is only used in the initial state; make sure it travelled with the run.
    assert repo_id


async def test_revise_loop_then_escalation_approve_gives_one_more_attempt(
    database: Any, tmp_path: Path
) -> None:
    reviewer = FakeReviewer(
        step_review_sequence=[
            make_review("revise", "change 1"),
            make_review("revise", "change 2"),
            make_review("revise", "change 3"),
        ]
    )
    fakes = make_fakes(plan=make_plan(1), reviewer=reviewer)
    async with session_scope(database.sessions) as session:
        parents = await make_ticket_parents(session)
        workspace_id = parents.workspace.id

    async with graph_env(database, tmp_path, fakes) as (manager, _deps, _settings):
        run_id = await manager.start(
            parents.ticket.id, workspace_id=workspace_id, created_by=parents.user.id
        )
        await wait_for_task(manager, run_id)
        await manager.resume(run_id, {"action": "approve"})
        await wait_for_task(manager, run_id)

        async with session_scope(database.sessions) as session:
            run = await _run(session, workspace_id, run_id)
            assert run.status == RunStatus.AWAITING_APPROVAL.value
            assert (
                await _approval_count(session, workspace_id, run_id, ApprovalKind.ESCALATION.value)
                == 1
            )

        assert len(fakes.steps.calls) == 3

        await manager.resume(run_id, {"action": "approve"})
        await wait_for_task(manager, run_id)
        assert len(fakes.steps.calls) == 4

        async with session_scope(database.sessions) as session:
            run = await _run(session, workspace_id, run_id)
            assert run.status == RunStatus.AWAITING_APPROVAL.value
            assert (
                await _approval_count(session, workspace_id, run_id, ApprovalKind.PR_OPEN.value)
                == 1
            )


async def test_escalation_reject_fails_run(database: Any, tmp_path: Path) -> None:
    reviewer = FakeReviewer(
        step_review_sequence=[
            make_review("revise", "change 1"),
            make_review("revise", "change 2"),
            make_review("revise", "change 3"),
        ]
    )
    fakes = make_fakes(plan=make_plan(1), reviewer=reviewer)
    async with session_scope(database.sessions) as session:
        parents = await make_ticket_parents(session)
        workspace_id = parents.workspace.id

    async with graph_env(database, tmp_path, fakes) as (manager, _deps, _settings):
        run_id = await manager.start(
            parents.ticket.id, workspace_id=workspace_id, created_by=parents.user.id
        )
        await wait_for_task(manager, run_id)
        await manager.resume(run_id, {"action": "approve"})
        await wait_for_task(manager, run_id)
        await manager.resume(run_id, {"action": "reject"})
        await wait_for_task(manager, run_id)

        async with session_scope(database.sessions) as session:
            run = await _run(session, workspace_id, run_id)
            assert run.status == RunStatus.FAILED.value
        # Lead review 12a (R5): the reject ends the graph cleanly through the path map.
        assert run_id not in manager._errors


async def test_rejecting_the_pr_ends_the_graph_cleanly(database: Any, tmp_path: Path) -> None:
    """Lead review 12a (R5): routes to END through the path map; no graph error."""
    fakes = make_fakes(plan=make_plan(1))
    async with session_scope(database.sessions) as session:
        parents = await make_ticket_parents(session)
        workspace_id = parents.workspace.id

    async with graph_env(database, tmp_path, fakes) as (manager, _deps, _settings):
        run_id = await manager.start(
            parents.ticket.id, workspace_id=workspace_id, created_by=parents.user.id
        )
        await wait_for_task(manager, run_id)
        await manager.resume(run_id, {"action": "approve"})  # the plan
        await wait_for_task(manager, run_id)
        await manager.resume(run_id, {"action": "reject"})  # the PR
        await wait_for_task(manager, run_id)

        async with session_scope(database.sessions) as session:
            run = await _run(session, workspace_id, run_id)
            assert run.status == RunStatus.CANCELLED.value
        assert run_id not in manager._errors
        assert fakes.pull_requests.open_calls == []


def test_every_conditional_edge_is_declared() -> None:
    """The diagram (and LangGraph's validation) need every route target declared."""
    graph = build_graph(cast(Any, SimpleNamespace(settings=None)))
    edges = {(edge.source, edge.target) for edge in graph.get_graph().edges}

    for source in ("approve_plan", "approve_pr", "approve_merge", "escalation", "watch_ci"):
        assert (source, "__end__") in edges
    assert ("lead_review_step", "escalation") in edges
    assert ("git_commit_step", "lead_final_review") in edges


async def test_resuming_plan_approval_does_not_duplicate_approval_row(
    database: Any, tmp_path: Path
) -> None:
    fakes = make_fakes(plan=make_plan(1))
    async with session_scope(database.sessions) as session:
        parents = await make_ticket_parents(session)
        workspace_id = parents.workspace.id

    async with graph_env(database, tmp_path, fakes) as (manager, _deps, _settings):
        run_id = await manager.start(
            parents.ticket.id, workspace_id=workspace_id, created_by=parents.user.id
        )
        await wait_for_task(manager, run_id)
        await manager.resume(run_id, {"action": "reject"})
        await wait_for_task(manager, run_id)

        async with session_scope(database.sessions) as session:
            approvals = ScopedRepo(session, Approval, workspace_id)
            rows = await approvals.list(run_id=run_id, idempotency_key="approve_plan:0:1")
            assert len(rows) == 1
            assert rows[0].status == ApprovalStatus.REJECTED.value


async def test_exhausted_budget_pauses_and_resume_with_raised_budget_continues(
    database: Any, tmp_path: Path
) -> None:
    fakes = make_fakes(plan=make_plan(1))
    settings = Settings(
        data_dir=tmp_path / "gaia-data", checkpoint_path=tmp_path / "checkpoints.sqlite"
    )
    deps = make_deps(database, settings, fakes)

    async with session_scope(database.sessions) as session:
        parents = await make_ticket_parents(session)
        workspace_id = parents.workspace.id
        run_id = "run-budget"
        runs = ScopedRepo(session, Run, workspace_id)
        await runs.create(
            id=run_id,
            ticket_id=parents.ticket.id,
            repo_id=parents.repository.id,
            budget_usd=Decimal("0.000001"),
            cost_usd=Decimal("0.000001"),
            graph_thread_id=run_id,
            created_by=parents.user.id,
        )

    async with open_checkpointer(settings) as saver:
        graph = build_graph(deps, checkpointer=saver)
        state = initial_state(
            run_id,
            workspace_id=workspace_id,
            ticket_id=parents.ticket.id,
            repo_id=parents.repository.id,
        )
        await graph.ainvoke(state, run_config(run_id), durability="sync")

        async with session_scope(database.sessions) as session:
            run = await _run(session, workspace_id, run_id)
            assert run.status == RunStatus.PAUSED_BUDGET.value
            assert (
                await _approval_count(session, workspace_id, run_id, ApprovalKind.BUDGET.value) == 1
            )

        command: dict[str, Any] = {"action": "approve", "budget_usd": Decimal("10.0")}
        await graph.ainvoke(Command[Any](resume=command), run_config(run_id), durability="sync")

        async with session_scope(database.sessions) as session:
            run = await _run(session, workspace_id, run_id)
            assert run.status == RunStatus.AWAITING_APPROVAL.value
            assert run.budget_usd == Decimal("10.0")
            assert fakes.worktrees.calls == [run_id]


async def test_crash_recovery_resumes_without_duplicate_rows(database: Any, tmp_path: Path) -> None:
    block_event = asyncio.Event()
    step_runner = FakeStepRunner(block_event=block_event, block_step=0)
    fakes = make_fakes(plan=make_plan(1), step_runner=step_runner)
    settings = Settings(
        data_dir=tmp_path / "gaia-data", checkpoint_path=tmp_path / "checkpoints.sqlite"
    )
    deps = make_deps(database, settings, fakes)

    async with session_scope(database.sessions) as session:
        parents = await make_ticket_parents(session)
        workspace_id = parents.workspace.id
        ticket_id = parents.ticket.id

    async with open_checkpointer(settings) as saver:
        graph = build_graph(deps, checkpointer=saver)
        manager = RunManager(graph, deps)
        run_id = await manager.start(
            ticket_id, workspace_id=workspace_id, created_by=parents.user.id
        )
        await wait_for_task(manager, run_id)
        await manager.resume(run_id, {"action": "approve"})
        await wait_for_step_calls(step_runner, 1)

        task = manager._tasks[run_id]
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await manager.aclose()

    block_event.set()

    async with open_checkpointer(settings) as saver:
        graph = build_graph(deps, checkpointer=saver)
        manager = RunManager(graph, deps)
        await manager.recover()
        await wait_for_task(manager, run_id)

        async with session_scope(database.sessions) as session:
            run = await _run(session, workspace_id, run_id)
            assert run.status == RunStatus.AWAITING_APPROVAL.value
            assert (
                await _approval_count(session, workspace_id, run_id, ApprovalKind.PR_OPEN.value)
                == 1
            )

        await manager.resume(run_id, {"action": "approve"})
        await wait_for_task(manager, run_id)
        await manager.resume(run_id, {"action": "approve"})
        await wait_for_task(manager, run_id)

        async with session_scope(database.sessions) as session:
            run = await _run(session, workspace_id, run_id)
            assert run.status == RunStatus.COMPLETED.value
            step_rows = ScopedRepo(session, RunStep, workspace_id).list(run_id=run_id)
            assert len(await step_rows) == 1
            approvals = ScopedRepo(session, Approval, workspace_id)
            approval_rows = await approvals.list(run_id=run_id)
            keys = [row.idempotency_key for row in approval_rows]
            assert len(keys) == len(set(keys))

        await manager.aclose()

    assert len([call for call in step_runner.calls if call[1] == 0]) == 2


async def test_concurrency_limit_keeps_second_run_pending(database: Any, tmp_path: Path) -> None:
    block_event = asyncio.Event()
    step_runner = FakeStepRunner(block_event=block_event, block_step=0)
    fakes = make_fakes(plan=make_plan(1), step_runner=step_runner)

    async with session_scope(database.sessions) as session:
        parents = await make_ticket_parents(session, slug="local")
        workspace_id = parents.workspace.id

    async with graph_env(database, tmp_path, fakes, max_concurrent_runs=1) as (
        manager,
        _deps,
        _settings,
    ):
        run_id_1 = await manager.start(
            parents.ticket.id, workspace_id=workspace_id, created_by=parents.user.id
        )
        await wait_for_task(manager, run_id_1)
        await manager.resume(run_id_1, {"action": "approve"})
        await wait_for_step_calls(step_runner, 1)

        run_id_2 = await manager.start(
            parents.ticket.id, workspace_id=workspace_id, created_by=parents.user.id
        )
        async with session_scope(database.sessions) as session:
            run2 = await _run(session, workspace_id, run_id_2)
            assert run2.status == RunStatus.PENDING.value

        block_event.set()
        await wait_for_task(manager, run_id_1)
        await wait_for_task(manager, run_id_2)

        async with session_scope(database.sessions) as session:
            run2 = await _run(session, workspace_id, run_id_2)
            assert run2.status == RunStatus.AWAITING_APPROVAL.value
            assert (
                await _approval_count(session, workspace_id, run_id_2, ApprovalKind.PLAN.value) == 1
            )


async def test_run_events_exist_for_completed_run(database: Any, tmp_path: Path) -> None:
    """A happy-path run also leaves the node and approval events behind."""
    fakes = make_fakes(plan=make_plan(1))
    async with session_scope(database.sessions) as session:
        parents = await make_ticket_parents(session)
        workspace_id = parents.workspace.id

    async with graph_env(database, tmp_path, fakes) as (manager, _deps, _settings):
        run_id = await manager.start(
            parents.ticket.id, workspace_id=workspace_id, created_by=parents.user.id
        )
        await wait_for_task(manager, run_id)
        await manager.resume(run_id, {"action": "approve"})
        await wait_for_task(manager, run_id)
        await manager.resume(run_id, {"action": "approve"})
        await wait_for_task(manager, run_id)
        await manager.resume(run_id, {"action": "approve"})
        await wait_for_task(manager, run_id)

    async with session_scope(database.sessions) as session:
        events = EventRepo(session, Event, workspace_id).list_for_run(run_id)
        types = [event.type for event in await events]
        assert EventType.RUN_NODE_STARTED.value in types
        assert EventType.RUN_NODE_FINISHED.value in types
        assert EventType.APPROVAL_REQUESTED.value in types
        assert EventType.APPROVAL_DECIDED.value in types


async def test_an_unknown_route_raises_instead_of_silently_ending(
    database: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Lead review 12a (R5): with path maps a typo'd route is an error, not a quiet stop."""
    fakes = make_fakes(plan=make_plan(1))
    async with session_scope(database.sessions) as session:
        parents = await make_ticket_parents(session)
        workspace_id = parents.workspace.id

    async def typo_route(state: RunState, deps: GraphDeps) -> dict[str, Any]:
        return {"route": "implment_step"}

    monkeypatch.setattr("gaia.orchestrator.graph.approve_plan", typo_route)
    async with graph_env(database, tmp_path, fakes) as (manager, _deps, _settings):
        run_id = await manager.start(
            parents.ticket.id, workspace_id=workspace_id, created_by=parents.user.id
        )
        await wait_for_task(manager, run_id)

        assert run_id in manager._errors
