"""Direct unit tests for :class:`gaia.orchestrator.runner.RunManager`."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from gaia.config.settings import Settings
from gaia.db.engine import session_scope
from gaia.db.ids import uuid7
from gaia.db.models import Event, Run
from gaia.db.repo import EventRepo, RunRepo, ScopedRepo
from gaia.domain.enums import EventType, RunStatus
from gaia.orchestrator.deps import GraphDeps
from gaia.orchestrator.graph import build_graph
from gaia.orchestrator.runner import RunBusy, RunManager, _initial_state
from tests.orchestrator_helpers import make_fakes, make_graph_deps, make_ticket_parents


@dataclass(frozen=True)
class RunnerEnv:
    deps: GraphDeps
    settings: Settings
    manager: RunManager
    workspace_id: str
    user_id: str
    ticket_id: str
    repo_id: str


@asynccontextmanager
async def runner_env(
    database: Any,
    tmp_path: Path,
    *,
    max_concurrent_runs: int = 3,
) -> AsyncIterator[RunnerEnv]:
    """Create a manager plus the rows needed to start or cancel a run."""
    settings = Settings(
        data_dir=tmp_path / "gaia-data",
        max_concurrent_runs=max_concurrent_runs,
    )
    fakes = make_fakes()
    deps = make_graph_deps(database, settings, fakes)
    graph = build_graph(deps)
    manager = RunManager(graph, deps, max_concurrent_runs=max_concurrent_runs)
    async with session_scope(database.sessions) as session:
        parents = await make_ticket_parents(session)
    try:
        yield RunnerEnv(
            deps=deps,
            settings=settings,
            manager=manager,
            workspace_id=parents.workspace.id,
            user_id=parents.user.id,
            ticket_id=parents.ticket.id,
            repo_id=parents.repository.id,
        )
    finally:
        await manager.aclose()


async def add_run(env: RunnerEnv, *, status: RunStatus = RunStatus.PENDING) -> str:
    """Create a run row for ``env`` and return its id."""
    run_id = uuid7()
    async with session_scope(env.deps.sessions) as session:
        runs = ScopedRepo(session, Run, env.workspace_id)
        await runs.create(
            id=run_id,
            ticket_id=env.ticket_id,
            repo_id=env.repo_id,
            budget_usd=Decimal("10.0"),
            graph_thread_id=run_id,
            created_by=env.user_id,
        )
        if status is not RunStatus.PENDING:
            run = await runs.get(run_id)
            assert run is not None
            from gaia.domain.state import transition

            transition(run, status)
    return run_id


async def test_start_rejects_a_missing_ticket(database: Any, tmp_path: Path) -> None:
    async with runner_env(database, tmp_path) as env:
        with pytest.raises(ValueError, match="no ticket"):
            await env.manager.start(
                "missing-ticket",
                workspace_id=env.workspace_id,
                created_by=env.user_id,
            )


async def test_cancel_cancels_the_task_and_marks_the_run_cancelled(
    database: Any, tmp_path: Path
) -> None:
    async with runner_env(database, tmp_path) as env:
        run_id = await add_run(env)
        env.manager._tasks[run_id] = asyncio.create_task(asyncio.Event().wait())

        await env.manager.cancel(run_id, workspace_id=env.workspace_id)

        assert env.manager._tasks[run_id].cancelled()
        async with session_scope(env.deps.sessions) as session:
            run = await RunRepo(session).get_by_id(run_id)
            assert run is not None
            assert run.status == RunStatus.CANCELLED.value
            events = EventRepo(session, Event, env.workspace_id).list_for_run(run_id)
            types = [event.type for event in await events]
            assert EventType.RUN_STATUS_CHANGED.value in types


async def test_cancel_unknown_run_raises(database: Any, tmp_path: Path) -> None:
    async with runner_env(database, tmp_path) as env:
        with pytest.raises(ValueError, match="no run"):
            await env.manager.cancel("missing-run", workspace_id=env.workspace_id)


async def test_cancel_skips_an_already_finished_task(database: Any, tmp_path: Path) -> None:
    async with runner_env(database, tmp_path) as env:
        run_id = await add_run(env)
        task = asyncio.create_task(asyncio.sleep(0))
        await task
        env.manager._tasks[run_id] = task

        await env.manager.cancel(run_id, workspace_id=env.workspace_id)

        async with session_scope(env.deps.sessions) as session:
            run = await RunRepo(session).get_by_id(run_id)
            assert run is not None
            assert run.status == RunStatus.CANCELLED.value


async def test_recover_skips_runs_already_active(database: Any, tmp_path: Path) -> None:
    async with runner_env(database, tmp_path) as env:
        run_id = await add_run(env)
        env.manager._tasks[run_id] = asyncio.create_task(asyncio.Event().wait())

        calls: list[str] = []

        async def fake_aget_state(config: Any) -> Any:
            calls.append("aget_state")
            return SimpleNamespace(interrupts=[], tasks=[], values={})

        monkeypatch = pytest.MonkeyPatch()
        monkeypatch.setattr(env.manager.graph, "aget_state", fake_aget_state)
        await env.manager.recover()
        monkeypatch.undo()

        assert calls == []


async def test_recover_marks_a_pending_interrupt_as_awaiting_approval(
    database: Any, tmp_path: Path
) -> None:
    async with runner_env(database, tmp_path) as env:
        run_id = await add_run(env, status=RunStatus.PLANNING)

        async def fake_aget_state(config: Any) -> Any:
            return SimpleNamespace(interrupts=[object()], tasks=[], values={})

        monkeypatch = pytest.MonkeyPatch()
        monkeypatch.setattr(env.manager.graph, "aget_state", fake_aget_state)
        await env.manager.recover()
        monkeypatch.undo()

        async with session_scope(env.deps.sessions) as session:
            run = await RunRepo(session).get_by_id(run_id)
            assert run is not None
            assert run.status == RunStatus.AWAITING_APPROVAL.value


async def test_recover_ignores_a_run_deleted_before_its_status_update(
    database: Any, tmp_path: Path
) -> None:
    async with runner_env(database, tmp_path) as env:
        run_id = await add_run(env, status=RunStatus.PLANNING)

        async def fake_aget_state(config: Any) -> Any:
            return SimpleNamespace(interrupts=[object()], tasks=[], values={})

        async def fake_get_by_id(self: Any, run_id: str) -> None:
            return None

        monkeypatch = pytest.MonkeyPatch()
        monkeypatch.setattr(env.manager.graph, "aget_state", fake_aget_state)
        monkeypatch.setattr("gaia.orchestrator.runner.RunRepo.get_by_id", fake_get_by_id)
        await env.manager.recover()
        monkeypatch.undo()

        async with session_scope(env.deps.sessions) as session:
            run = await RunRepo(session).get_by_id(run_id)
            assert run is not None
            assert run.status == RunStatus.PLANNING.value


async def test_recover_resumes_a_run_with_checkpoint_values(database: Any, tmp_path: Path) -> None:
    async with runner_env(database, tmp_path) as env:
        run_id = await add_run(env)

        async def fake_aget_state(config: Any) -> Any:
            return SimpleNamespace(interrupts=[], tasks=[], values={"run_id": run_id})

        launched: list[tuple[str, Any]] = []
        env.manager._launch = lambda run_id, input_: launched.append((run_id, input_))  # type: ignore[method-assign]
        monkeypatch = pytest.MonkeyPatch()
        monkeypatch.setattr(env.manager.graph, "aget_state", fake_aget_state)
        await env.manager.recover()
        monkeypatch.undo()

        assert launched == [(run_id, None)]


async def test_recover_starts_a_run_without_checkpoint_values_from_scratch(
    database: Any, tmp_path: Path
) -> None:
    async with runner_env(database, tmp_path) as env:
        run_id = await add_run(env)

        async def fake_aget_state(config: Any) -> Any:
            return SimpleNamespace(interrupts=[], tasks=[], values={})

        launched: list[tuple[str, Any]] = []
        env.manager._launch = lambda run_id, input_: launched.append((run_id, input_))  # type: ignore[method-assign]
        monkeypatch = pytest.MonkeyPatch()
        monkeypatch.setattr(env.manager.graph, "aget_state", fake_aget_state)
        await env.manager.recover()
        monkeypatch.undo()

        assert len(launched) == 1
        assert launched[0][0] == run_id
        assert launched[0][1] == _initial_state(
            run_id,
            env.ticket_id,
            env.repo_id,
            env.workspace_id,
        )


async def test_aclose_with_no_tasks_is_a_noop(database: Any, tmp_path: Path) -> None:
    async with runner_env(database, tmp_path) as env:
        await env.manager.aclose()
        assert env.manager.active() == []


async def test_launch_records_an_invocation_error(database: Any, tmp_path: Path) -> None:
    async with runner_env(database, tmp_path) as env:

        async def fake_ainvoke(input_: Any, config: Any, *, durability: str) -> None:
            raise RuntimeError("invocation failed")

        monkeypatch = pytest.MonkeyPatch()
        monkeypatch.setattr(env.manager.graph, "ainvoke", fake_ainvoke)
        env.manager._launch("run-error", {"run_id": "run-error"})
        await env.manager._tasks["run-error"]
        monkeypatch.undo()

        assert isinstance(env.manager._errors["run-error"], RuntimeError)


async def test_resume_refuses_a_run_that_is_still_executing(database: Any, tmp_path: Path) -> None:
    """Lead review 12a (R1): two ainvoke calls on one checkpoint thread would race."""
    async with runner_env(database, tmp_path) as env:
        run_id = await add_run(env)
        env.manager._tasks[run_id] = asyncio.create_task(asyncio.Event().wait())

        with pytest.raises(RunBusy):
            await env.manager.resume(run_id, {"action": "approve"})


async def test_cancel_refuses_a_run_from_another_workspace(database: Any, tmp_path: Path) -> None:
    """Lead review 12a (R3): cancel is workspace-scoped."""
    async with runner_env(database, tmp_path) as env:
        run_id = await add_run(env)

        with pytest.raises(ValueError, match="no run"):
            await env.manager.cancel(run_id, workspace_id="another-workspace")


async def test_graph_errors_are_logged_and_finished_tasks_pruned(
    database: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Lead review 12a (R2, R4)."""
    logged: list[dict[str, Any]] = []
    monkeypatch.setattr(
        "gaia.orchestrator.runner.logger",
        SimpleNamespace(error=lambda event, **kw: logged.append({"event": event, **kw})),
    )
    async with runner_env(database, tmp_path) as env:

        async def boom(*args: Any, **kwargs: Any) -> None:
            raise RuntimeError("checkpoint unavailable")

        monkeypatch.setattr(env.manager.graph, "ainvoke", boom)
        first = await add_run(env)
        env.manager._launch(first, None)
        await env.manager._tasks[first]
        second = await add_run(env)
        env.manager._launch(second, None)
        await env.manager._tasks[second]

        assert [entry["error_class"] for entry in logged] == ["RuntimeError", "RuntimeError"]
        assert first not in env.manager._tasks
        assert isinstance(env.manager._errors[first], RuntimeError)
