"""RunManager kill-switch tests (doc 18a)."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from gaia.config.settings import Settings
from gaia.db.engine import session_scope
from gaia.db.ids import uuid7
from gaia.db.models import Approval, Event, Run
from gaia.db.repo import EventRepo, RunRepo, ScopedRepo
from gaia.domain.enums import ApprovalKind, ApprovalStatus, EventType, RunStatus
from gaia.domain.state import transition
from gaia.orchestrator.deps import GraphDeps
from gaia.orchestrator.graph import build_graph
from gaia.orchestrator.pause import OrchestratorPaused, PauseSwitch
from gaia.orchestrator.runner import RunManager, open_checkpointer
from tests.orchestrator_fakes import FakeStepRunner, make_plan
from tests.orchestrator_helpers import make_fakes, make_ticket_parents
from tests.test_orchestrator_graph import make_deps, wait_for_step_calls, wait_for_task

pytestmark = pytest.mark.anyio


@asynccontextmanager
async def pause_env(
    database: Any,
    tmp_path: Path,
    fakes: Any,
) -> AsyncIterator[tuple[RunManager, GraphDeps, Settings, PauseSwitch]]:
    """Open a real graph and manager with a pause switch on a temp checkpointer."""
    settings = Settings(
        data_dir=tmp_path / "gaia-data",
        checkpoint_path=tmp_path / "checkpoints.sqlite",
    )
    deps = make_deps(database, settings, fakes)
    pause = PauseSwitch(settings.data_dir / "paused")
    async with open_checkpointer(settings) as saver:
        graph = build_graph(deps, checkpointer=saver)
        manager = RunManager(graph, deps, pause=pause)
        try:
            yield manager, deps, settings, pause
        finally:
            await manager.aclose()


async def _add_run(
    deps: GraphDeps,
    *,
    workspace_id: str,
    ticket_id: str,
    repo_id: str,
    user_id: str,
    status: RunStatus = RunStatus.PENDING,
) -> str:
    """Create a run row with no live task."""
    run_id = uuid7()
    async with session_scope(deps.sessions) as session:
        runs = ScopedRepo(session, Run, workspace_id)
        await runs.create(
            id=run_id,
            ticket_id=ticket_id,
            repo_id=repo_id,
            budget_usd=Decimal("10.0"),
            graph_thread_id=run_id,
            created_by=user_id,
            status=status.value,
        )
    return run_id


async def _parents(database: Any):
    async with session_scope(database.sessions) as session:
        return await make_ticket_parents(session)


async def test_start_and_resume_are_refused_while_paused(database: Any, tmp_path: Path) -> None:
    async with pause_env(database, tmp_path, make_fakes()) as (manager, deps, _settings, pause):
        parents = await _parents(database)
        run_id = await _add_run(
            deps,
            workspace_id=parents.workspace.id,
            ticket_id=parents.ticket.id,
            repo_id=parents.repository.id,
            user_id=parents.user.id,
        )
        pause.pause("do not start")

        with pytest.raises(OrchestratorPaused):
            await manager.start(
                parents.ticket.id,
                workspace_id=parents.workspace.id,
                created_by=parents.user.id,
            )
        with pytest.raises(OrchestratorPaused):
            await manager.resume(run_id, {"action": "approve"})


async def test_pause_all_cancels_a_blocked_mid_node_run(database: Any, tmp_path: Path) -> None:
    block_event = asyncio.Event()
    step_runner = FakeStepRunner(block_event=block_event, block_step=0)
    fakes = make_fakes(plan=make_plan(1), step_runner=step_runner)

    async with pause_env(database, tmp_path, fakes) as (manager, _deps, _settings, _pause):
        parents = await _parents(database)
        run_id = await manager.start(
            parents.ticket.id,
            workspace_id=parents.workspace.id,
            created_by=parents.user.id,
        )
        await wait_for_task(manager, run_id)
        await manager.resume(run_id, {"action": "approve"})
        await wait_for_step_calls(step_runner, 1)

        paused = await manager.pause_all(reason="dinner")

        assert paused == [run_id]
        assert manager._tasks[run_id].cancelled()
        async with session_scope(database.sessions) as session:
            run = await RunRepo(session).get_by_id(run_id)
            assert run is not None
            assert run.status == RunStatus.PAUSED.value


async def test_pause_all_pauses_a_pending_run_without_a_task(database: Any, tmp_path: Path) -> None:
    async with pause_env(database, tmp_path, make_fakes()) as (manager, deps, _settings, _pause):
        parents = await _parents(database)
        run_id = await _add_run(
            deps,
            workspace_id=parents.workspace.id,
            ticket_id=parents.ticket.id,
            repo_id=parents.repository.id,
            user_id=parents.user.id,
        )

        assert await manager.pause_all(reason="stop") == [run_id]

        async with session_scope(database.sessions) as session:
            run = await RunRepo(session).get_by_id(run_id)
            assert run is not None
            assert run.status == RunStatus.PAUSED.value
            events = EventRepo(session, Event, parents.workspace.id).list_for_run(run_id)
            status_events = [
                event for event in await events if event.type == EventType.RUN_STATUS_CHANGED.value
            ]
            assert status_events[-1].payload_json == {"run_id": run_id, "status": "paused"}


async def test_pause_all_skips_a_live_task_whose_run_row_is_gone(
    database: Any, tmp_path: Path
) -> None:
    async with pause_env(database, tmp_path, make_fakes()) as (manager, _deps, _settings, _pause):
        manager._tasks["missing-run"] = asyncio.create_task(asyncio.Event().wait())

        assert await manager.pause_all(reason="stop") == []

        assert manager._tasks["missing-run"].cancelled()


async def test_pause_all_leaves_awaiting_approval_and_gate_approval_alone(
    database: Any, tmp_path: Path
) -> None:
    async with pause_env(database, tmp_path, make_fakes()) as (manager, deps, _settings, _pause):
        parents = await _parents(database)
        run_id = await _add_run(
            deps,
            workspace_id=parents.workspace.id,
            ticket_id=parents.ticket.id,
            repo_id=parents.repository.id,
            user_id=parents.user.id,
            status=RunStatus.AWAITING_APPROVAL,
        )
        async with session_scope(database.sessions) as session:
            approvals = ScopedRepo(session, Approval, parents.workspace.id)
            gate = await approvals.create(
                run_id=run_id,
                idempotency_key="approve_plan:0:1",
                kind=ApprovalKind.PLAN.value,
                payload_json={"kind": "plan"},
                status=ApprovalStatus.PENDING.value,
            )

        assert await manager.pause_all(reason="stop") == []

        async with session_scope(database.sessions) as session:
            run = await RunRepo(session).get_by_id(run_id)
            assert run is not None
            assert run.status == RunStatus.AWAITING_APPROVAL.value
            row = await ScopedRepo(session, Approval, parents.workspace.id).get(gate.id)
            assert row is not None
            assert row.status == ApprovalStatus.PENDING.value


async def test_pause_all_expires_command_approval_and_publishes(
    database: Any, tmp_path: Path
) -> None:
    async with pause_env(database, tmp_path, make_fakes()) as (manager, deps, _settings, _pause):
        parents = await _parents(database)
        run_id = await _add_run(
            deps,
            workspace_id=parents.workspace.id,
            ticket_id=parents.ticket.id,
            repo_id=parents.repository.id,
            user_id=parents.user.id,
        )
        async with session_scope(database.sessions) as session:
            approvals = ScopedRepo(session, Approval, parents.workspace.id)
            command = await approvals.create(
                run_id=run_id,
                idempotency_key="command:1",
                kind=ApprovalKind.COMMAND.value,
                payload_json={"argv": ["pytest"]},
                status=ApprovalStatus.PENDING.value,
            )
            gate = await approvals.create(
                run_id=run_id,
                idempotency_key="escalation:0:1",
                kind=ApprovalKind.ESCALATION.value,
                payload_json={"kind": "escalation"},
                status=ApprovalStatus.PENDING.value,
            )

        await manager.pause_all(reason="stop")

        async with session_scope(database.sessions) as session:
            command_row = await ScopedRepo(session, Approval, parents.workspace.id).get(command.id)
            assert command_row is not None
            assert command_row.status == ApprovalStatus.EXPIRED.value
            assert command_row.comment == "paused"
            gate_row = await ScopedRepo(session, Approval, parents.workspace.id).get(gate.id)
            assert gate_row is not None
            assert gate_row.status == ApprovalStatus.PENDING.value
            events = EventRepo(session, Event, parents.workspace.id).list_for_run(run_id)
            decided = [
                event for event in await events if event.type == EventType.APPROVAL_DECIDED.value
            ]
            assert decided[-1].payload_json == {"approval_id": command.id, "action": "expired"}


async def test_resume_all_marks_a_paused_run_with_an_interrupt_awaiting_approval(
    database: Any, tmp_path: Path
) -> None:
    async with pause_env(database, tmp_path, make_fakes()) as (manager, _deps, _settings, _pause):
        parents = await _parents(database)
        run_id = await manager.start(
            parents.ticket.id,
            workspace_id=parents.workspace.id,
            created_by=parents.user.id,
        )
        await wait_for_task(manager, run_id)

        async with session_scope(database.sessions) as session:
            run = await RunRepo(session).get_by_id(run_id)
            assert run is not None
            transition(run, RunStatus.PAUSED)

        assert await manager.resume_all() == [run_id]

        async with session_scope(database.sessions) as session:
            run = await RunRepo(session).get_by_id(run_id)
            assert run is not None
            assert run.status == RunStatus.AWAITING_APPROVAL.value


async def test_resume_all_relaunches_a_paused_run_without_a_checkpoint(
    database: Any, tmp_path: Path
) -> None:
    fakes = make_fakes(plan=make_plan(2))
    async with pause_env(database, tmp_path, fakes) as (manager, deps, _settings, _pause):
        parents = await _parents(database)
        run_id = await _add_run(
            deps,
            workspace_id=parents.workspace.id,
            ticket_id=parents.ticket.id,
            repo_id=parents.repository.id,
            user_id=parents.user.id,
            status=RunStatus.PAUSED,
        )

        assert await manager.resume_all() == [run_id]
        await wait_for_task(manager, run_id)
        await manager.resume(run_id, {"action": "approve"})
        await wait_for_task(manager, run_id)
        await manager.resume(run_id, {"action": "approve"})
        await wait_for_task(manager, run_id)
        await manager.resume(run_id, {"action": "approve"})
        await wait_for_task(manager, run_id)

        async with session_scope(database.sessions) as session:
            run = await RunRepo(session).get_by_id(run_id)
            assert run is not None
            assert run.status == RunStatus.COMPLETED.value


async def test_recover_leaves_paused_runs_alone(database: Any, tmp_path: Path) -> None:
    async with pause_env(database, tmp_path, make_fakes()) as (manager, deps, _settings, _pause):
        parents = await _parents(database)
        run_id = await _add_run(
            deps,
            workspace_id=parents.workspace.id,
            ticket_id=parents.ticket.id,
            repo_id=parents.repository.id,
            user_id=parents.user.id,
            status=RunStatus.PAUSED,
        )

        await manager.recover()

        assert manager.active() == []
        async with session_scope(database.sessions) as session:
            run = await RunRepo(session).get_by_id(run_id)
            assert run is not None
            assert run.status == RunStatus.PAUSED.value


async def test_recover_does_nothing_while_the_flag_is_set(database: Any, tmp_path: Path) -> None:
    async with pause_env(database, tmp_path, make_fakes()) as (manager, deps, _settings, pause):
        parents = await _parents(database)
        run_id = await _add_run(
            deps,
            workspace_id=parents.workspace.id,
            ticket_id=parents.ticket.id,
            repo_id=parents.repository.id,
            user_id=parents.user.id,
        )
        pause.pause("startup hold")

        await manager.recover()

        assert manager.active() == []
        async with session_scope(database.sessions) as session:
            run = await RunRepo(session).get_by_id(run_id)
            assert run is not None
            assert run.status == RunStatus.PENDING.value


async def test_pause_all_and_resume_all_require_a_switch(database: Any, tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path / "gaia-data")
    deps = make_deps(database, settings, make_fakes())
    graph = build_graph(deps)
    manager = RunManager(graph, deps)
    try:
        with pytest.raises(RuntimeError, match="pause switch is not configured"):
            await manager.pause_all(reason="x")
        with pytest.raises(RuntimeError, match="pause switch is not configured"):
            await manager.resume_all()
    finally:
        await manager.aclose()
