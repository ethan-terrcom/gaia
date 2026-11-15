"""Direct unit tests for :func:`gaia.orchestrator.guard.with_guard`."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from langgraph.errors import GraphInterrupt

from gaia.config.settings import Settings
from gaia.db.engine import session_scope
from gaia.db.ids import uuid7
from gaia.db.models import Approval, Event, Run
from gaia.db.repo import EventRepo, ScopedRepo
from gaia.domain.enums import ApprovalKind, ApprovalStatus, EventType, RunStatus
from gaia.domain.state import transition
from gaia.orchestrator.deps import GraphDeps
from gaia.orchestrator.guard import RunFailed, _load_run, with_guard
from gaia.orchestrator.state import RunState
from tests.orchestrator_helpers import make_fakes, make_graph_deps, make_ticket_parents


@dataclass(frozen=True)
class GuardEnv:
    deps: GraphDeps
    settings: Settings
    workspace_id: str
    run_id: str
    ticket_id: str
    repo_id: str


def state_for(env: GuardEnv, **overrides: Any) -> RunState:
    state: RunState = {
        "run_id": env.run_id,
        "ticket_id": env.ticket_id,
        "repo_id": env.repo_id,
        "workspace_id": env.workspace_id,
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
    state.update(overrides)  # type: ignore[typeddict-item]
    return state


async def set_run_status(env: GuardEnv, *statuses: RunStatus) -> None:
    async with session_scope(env.deps.sessions) as session:
        run = await ScopedRepo(session, Run, env.workspace_id).get(env.run_id)
        assert run is not None
        for status in statuses:
            transition(run, status)


@asynccontextmanager
async def guard_env(
    database: Any,
    tmp_path: Path,
    *,
    budget_usd: str = "10.0",
    cost_usd: str = "0.0",
    statuses: tuple[RunStatus, ...] = (),
) -> AsyncIterator[GuardEnv]:
    """Create a run and graph dependencies for one direct guard test."""
    settings = Settings(data_dir=tmp_path / "gaia-data")
    fakes = make_fakes()
    deps = make_graph_deps(database, settings, fakes)
    async with session_scope(database.sessions) as session:
        parents = await make_ticket_parents(session)
        run_id = uuid7()
        runs = ScopedRepo(session, Run, parents.workspace.id)
        await runs.create(
            id=run_id,
            ticket_id=parents.ticket.id,
            repo_id=parents.repository.id,
            budget_usd=Decimal(budget_usd),
            cost_usd=Decimal(cost_usd),
            graph_thread_id=run_id,
            created_by=parents.user.id,
        )
    env = GuardEnv(
        deps=deps,
        settings=settings,
        workspace_id=parents.workspace.id,
        run_id=run_id,
        ticket_id=parents.ticket.id,
        repo_id=parents.repository.id,
    )
    if statuses:
        await set_run_status(env, *statuses)
    yield env


async def test_load_run_requires_ids(database: Any, tmp_path: Path) -> None:
    async with guard_env(database, tmp_path) as env:
        with pytest.raises(ValueError, match="requires run_id"):
            async with session_scope(env.deps.sessions) as session:
                await _load_run(session, {})


async def test_load_run_requires_an_existing_run(database: Any, tmp_path: Path) -> None:
    async with guard_env(database, tmp_path) as env:
        state = state_for(env, run_id="missing-run")
        with pytest.raises(ValueError, match="no run"):
            async with session_scope(env.deps.sessions) as session:
                await _load_run(session, state)


async def test_guard_does_nothing_for_a_terminal_run(database: Any, tmp_path: Path) -> None:
    async with guard_env(database, tmp_path) as env:
        await set_run_status(env, RunStatus.FAILED)
        calls: list[RunState] = []

        async def node(state: RunState) -> dict[str, Any]:
            calls.append(state)
            return {"called": True}

        wrapped = with_guard(env.deps, "terminal_node")(node)
        assert await wrapped(state_for(env)) == {"route": "end"}
        assert calls == []


async def test_guard_marks_a_failed_node_and_raises_run_failed(
    database: Any, tmp_path: Path
) -> None:
    async with guard_env(database, tmp_path) as env:

        async def node(state: RunState) -> dict[str, Any]:
            raise RuntimeError("node exploded")

        wrapped = with_guard(env.deps, "failing_node")(node)
        with pytest.raises(RunFailed) as raised:
            await wrapped(state_for(env))
        assert raised.value.run_id == env.run_id
        assert raised.value.node_name == "failing_node"
        assert "RuntimeError" in raised.value.error

        async with session_scope(env.deps.sessions) as session:
            run = await ScopedRepo(session, Run, env.workspace_id).get(env.run_id)
            assert run is not None
            assert run.status == RunStatus.FAILED.value
            events = EventRepo(session, Event, env.workspace_id).list_for_run(env.run_id)
            types = [event.type for event in await events]
            assert EventType.RUN_FAILED.value in types


async def test_guard_lets_langgraph_interrupts_bubble_up(database: Any, tmp_path: Path) -> None:
    async with guard_env(database, tmp_path) as env:

        async def node(state: RunState) -> dict[str, Any]:
            raise GraphInterrupt()

        wrapped = with_guard(env.deps, "interrupting_node")(node)
        with pytest.raises(GraphInterrupt):
            await wrapped(state_for(env))

        async with session_scope(env.deps.sessions) as session:
            run = await ScopedRepo(session, Run, env.workspace_id).get(env.run_id)
            assert run is not None
            assert run.status == RunStatus.PENDING.value


async def test_guard_budget_reject_cancels_the_run(database: Any, tmp_path: Path) -> None:
    async with guard_env(database, tmp_path, budget_usd="0.000001", cost_usd="0.000001") as env:
        calls: list[RunState] = []

        async def node(state: RunState) -> dict[str, Any]:
            calls.append(state)
            return {"called": True}

        monkeypatch = pytest.MonkeyPatch()
        monkeypatch.setattr(
            "gaia.orchestrator.guard.interrupt", lambda payload: {"action": "reject"}
        )
        wrapped = with_guard(env.deps, "budget_node")(node)
        assert await wrapped(state_for(env)) == {"route": "end"}
        monkeypatch.undo()
        assert calls == []

        async with session_scope(env.deps.sessions) as session:
            run = await ScopedRepo(session, Run, env.workspace_id).get(env.run_id)
            assert run is not None
            assert run.status == RunStatus.CANCELLED.value
            approvals = ScopedRepo(session, Approval, env.workspace_id)
            rows = await approvals.list(run_id=env.run_id, kind=ApprovalKind.BUDGET.value)
            assert len(rows) == 1
            assert rows[0].status == ApprovalStatus.REJECTED.value


async def test_guard_budget_approve_with_new_budget_continues(
    database: Any, tmp_path: Path
) -> None:
    async with guard_env(database, tmp_path, budget_usd="0.000001", cost_usd="0.000001") as env:

        async def node(state: RunState) -> dict[str, Any]:
            return {"called": True}

        monkeypatch = pytest.MonkeyPatch()
        monkeypatch.setattr(
            "gaia.orchestrator.guard.interrupt",
            lambda payload: {"action": "approve", "budget_usd": Decimal("20.0")},
        )
        wrapped = with_guard(env.deps, "budget_node")(node)
        assert await wrapped(state_for(env)) == {"called": True}
        monkeypatch.undo()

        async with session_scope(env.deps.sessions) as session:
            run = await ScopedRepo(session, Run, env.workspace_id).get(env.run_id)
            assert run is not None
            assert run.budget_usd == Decimal("20.0")


async def test_guard_budget_approve_without_a_new_budget_continues(
    database: Any, tmp_path: Path
) -> None:
    async with guard_env(database, tmp_path, budget_usd="0.000001", cost_usd="0.000001") as env:
        calls: list[RunState] = []

        async def node(state: RunState) -> dict[str, Any]:
            calls.append(state)
            return {"called": True}

        monkeypatch = pytest.MonkeyPatch()
        monkeypatch.setattr(
            "gaia.orchestrator.guard.interrupt", lambda payload: {"action": "approve"}
        )
        wrapped = with_guard(env.deps, "budget_node")(node)
        assert await wrapped(state_for(env)) == {"called": True}
        monkeypatch.undo()
        assert len(calls) == 1

        # Lead review 12a (G1): approve without a number grants one more default budget.
        async with session_scope(env.deps.sessions) as session:
            run = await ScopedRepo(session, Run, env.workspace_id).get(env.run_id)
            assert run is not None
            expected = Decimal("0.000001") + Decimal(str(env.deps.settings.run_budget_usd))
            assert run.budget_usd == expected


async def test_guard_budget_approve_rejects_a_budget_not_above_spend(
    database: Any, tmp_path: Path
) -> None:
    async with guard_env(database, tmp_path, budget_usd="0.000001", cost_usd="0.000001") as env:

        async def node(state: RunState) -> dict[str, Any]:
            return {"called": True}

        monkeypatch = pytest.MonkeyPatch()
        monkeypatch.setattr(
            "gaia.orchestrator.guard.interrupt",
            lambda payload: {"action": "approve", "budget_usd": Decimal("0")},
        )
        wrapped = with_guard(env.deps, "budget_node")(node)
        with pytest.raises(ValueError, match="above what the run has spent"):
            await wrapped(state_for(env))
        monkeypatch.undo()


async def test_guard_failure_expires_the_runs_pending_approvals(
    database: Any, tmp_path: Path
) -> None:
    """Live E2E fix A: a failed run leaves no approval in the inbox."""
    async with guard_env(database, tmp_path) as env:
        async with session_scope(env.deps.sessions) as session:
            approval = await ScopedRepo(session, Approval, env.workspace_id).create(
                run_id=env.run_id,
                idempotency_key="escalation:0:1",
                kind=ApprovalKind.ESCALATION.value,
                payload_json={"kind": "escalation"},
                status=ApprovalStatus.PENDING.value,
            )
            approval_id = approval.id

        async def node(state: RunState) -> dict[str, Any]:
            raise RuntimeError("node exploded")

        with pytest.raises(RunFailed):
            await with_guard(env.deps, "failing_node")(node)(state_for(env))

        async with session_scope(env.deps.sessions) as session:
            row = await ScopedRepo(session, Approval, env.workspace_id).get(approval_id)
            assert row is not None
            assert row.status == ApprovalStatus.EXPIRED.value
            assert row.comment == "run failed"
            events = await EventRepo(session, Event, env.workspace_id).list_for_run(env.run_id)
            decided = [e for e in events if e.type == EventType.APPROVAL_DECIDED.value]
            assert decided[-1].payload_json == {"approval_id": approval_id, "action": "expired"}
