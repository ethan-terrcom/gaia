"""Daily-budget guard and POST /runs tests (doc 18a)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from gaia.db.engine import session_scope
from gaia.db.models import Approval, LlmUsage, Run, Ticket
from gaia.db.repo import ScopedRepo
from gaia.domain.enums import ApprovalKind, ApprovalStatus, RunStatus
from gaia.orchestrator.guard import with_guard
from gaia.runtimes.approvals import DbApprovalBroker
from tests.conftest import Api, Database
from tests.test_api_runs import _manager, _seed_ticket_worktree
from tests.test_orchestrator_guard import guard_env, state_for

pytestmark = pytest.mark.anyio


async def _add_usage(
    env: Any,
    *,
    cost_usd: Decimal,
    created_at: datetime,
) -> None:
    """Write one ``llm_usage`` row for the guard's daily-spend query."""
    async with session_scope(env.deps.sessions) as session:
        usages = ScopedRepo(session, LlmUsage, env.workspace_id)
        await usages.create(
            run_id=env.run_id,
            provider="test",
            model="test-model",
            input_tokens=0,
            output_tokens=0,
            cache_read_tokens=0,
            cache_creation_tokens=0,
            cost_usd=cost_usd,
            created_at=created_at,
        )


async def test_daily_cap_pauses_with_daily_payload(database: Any, tmp_path: Path) -> None:
    frozen = datetime(2026, 11, 14, 12, 0, tzinfo=UTC)
    async with guard_env(database, tmp_path, budget_usd="100.0", cost_usd="0.0") as env:
        await _add_usage(env, cost_usd=Decimal("20.0"), created_at=frozen)
        captured: list[dict[str, Any]] = []

        async def node(state: Any) -> dict[str, Any]:
            return {"called": True}

        def fake_interrupt(payload: dict[str, Any]) -> dict[str, Any]:
            captured.append(payload)
            return {"action": "approve"}

        monkeypatch = pytest.MonkeyPatch()
        monkeypatch.setattr("gaia.orchestrator.guard.interrupt", fake_interrupt)
        wrapped = with_guard(env.deps, "daily_node", now=lambda: frozen)(node)
        assert await wrapped(state_for(env)) == {"called": True}
        monkeypatch.undo()

        assert captured[0]["scope"] == "daily"
        assert captured[0]["spent_today_usd"] == "20.000000"
        assert captured[0]["daily_budget_usd"] == "20.0"

        async with session_scope(env.deps.sessions) as session:
            approvals = ScopedRepo(session, Approval, env.workspace_id)
            rows = await approvals.list(run_id=env.run_id, kind=ApprovalKind.BUDGET.value)
            assert len(rows) == 1
            row = rows[0]
            assert row.payload_json["scope"] == "daily"
            assert row.payload_json["spent_today_usd"] == "20.000000"
            assert row.payload_json["daily_budget_usd"] == "20.0"
            assert row.idempotency_key == "budget:daily_node:daily:0"
            assert row.status == ApprovalStatus.APPROVED.value
            run = await ScopedRepo(session, Run, env.workspace_id).get(env.run_id)
            assert run is not None
            assert run.budget_usd == Decimal("100.0")


async def test_run_scope_wins_when_both_caps_are_spent(database: Any, tmp_path: Path) -> None:
    frozen = datetime(2026, 11, 14, 12, 0, tzinfo=UTC)
    async with guard_env(database, tmp_path, budget_usd="1.0", cost_usd="1.0") as env:
        await _add_usage(env, cost_usd=Decimal("20.0"), created_at=frozen)
        captured: list[dict[str, Any]] = []

        async def node(state: Any) -> dict[str, Any]:
            return {"called": True}

        def fake_interrupt(payload: dict[str, Any]) -> dict[str, Any]:
            captured.append(payload)
            return {"action": "approve", "budget_usd": Decimal("2.0")}

        monkeypatch = pytest.MonkeyPatch()
        monkeypatch.setattr("gaia.orchestrator.guard.interrupt", fake_interrupt)
        wrapped = with_guard(env.deps, "run_node", now=lambda: frozen)(node)
        assert await wrapped(state_for(env)) == {"called": True}
        monkeypatch.undo()

        assert captured[0]["scope"] == "run"
        assert "spent_today_usd" not in captured[0]
        async with session_scope(env.deps.sessions) as session:
            run = await ScopedRepo(session, Run, env.workspace_id).get(env.run_id)
            assert run is not None
            assert run.budget_usd == Decimal("2.0")


async def test_two_daily_pauses_create_two_rows_and_ignore_budget(
    database: Any, tmp_path: Path
) -> None:
    frozen = datetime(2026, 11, 14, 12, 0, tzinfo=UTC)
    async with guard_env(database, tmp_path, budget_usd="100.0", cost_usd="0.0") as env:
        await _add_usage(env, cost_usd=Decimal("20.0"), created_at=frozen)

        async def node(state: Any) -> dict[str, Any]:
            return {}

        monkeypatch = pytest.MonkeyPatch()
        monkeypatch.setattr(
            "gaia.orchestrator.guard.interrupt",
            lambda payload: {"action": "approve", "budget_usd": Decimal("999.0")},
        )
        wrapped = with_guard(env.deps, "node", now=lambda: frozen)(node)
        await wrapped(state_for(env))
        await wrapped(state_for(env))
        monkeypatch.undo()

        async with session_scope(env.deps.sessions) as session:
            rows = await ScopedRepo(session, Approval, env.workspace_id).list(
                run_id=env.run_id, kind=ApprovalKind.BUDGET.value
            )
            assert sorted(row.idempotency_key for row in rows) == [
                "budget:node:daily:0",
                "budget:node:daily:1",
            ]
            assert {row.status for row in rows} == {ApprovalStatus.APPROVED.value}
            run = await ScopedRepo(session, Run, env.workspace_id).get(env.run_id)
            assert run is not None
            assert run.budget_usd == Decimal("100.0")


async def test_daily_reject_cancels_the_run(database: Any, tmp_path: Path) -> None:
    frozen = datetime(2026, 11, 14, 12, 0, tzinfo=UTC)
    async with guard_env(database, tmp_path, budget_usd="100.0", cost_usd="0.0") as env:
        await _add_usage(env, cost_usd=Decimal("20.0"), created_at=frozen)

        async def node(state: Any) -> dict[str, Any]:
            return {"called": True}

        monkeypatch = pytest.MonkeyPatch()
        monkeypatch.setattr(
            "gaia.orchestrator.guard.interrupt",
            lambda payload: {"action": "reject"},
        )
        wrapped = with_guard(env.deps, "daily_node", now=lambda: frozen)(node)
        assert await wrapped(state_for(env)) == {"route": "end"}
        monkeypatch.undo()

        async with session_scope(env.deps.sessions) as session:
            run = await ScopedRepo(session, Run, env.workspace_id).get(env.run_id)
            assert run is not None
            assert run.status == RunStatus.CANCELLED.value
            rows = await ScopedRepo(session, Approval, env.workspace_id).list(
                run_id=env.run_id, kind=ApprovalKind.BUDGET.value
            )
            assert rows[0].status == ApprovalStatus.REJECTED.value


@pytest.fixture
async def run_api(api: Api, database: Database) -> AsyncIterator[Api]:
    """The real app with a fake-graph manager for POST /runs daily-cap tests."""
    ticket_id = await _seed_ticket_worktree(api, database, name="daily-budget-api")
    api.app.state.extra_test_ticket_id = ticket_id
    original = api.app.state.services
    async with _manager(database, api.settings) as (manager, deps):
        broker = DbApprovalBroker(database.sessions, deps.events)
        api.app.state.services = replace(original, runs=manager, approvals=broker)
        try:
            yield api
        finally:
            api.app.state.services = original


async def _seed_completed_run_with_usage(
    database: Database,
    api: Api,
    *,
    ticket_id: str,
    cost_usd: Decimal,
) -> None:
    workspace_id = api.app.state.services.principal.workspace_id
    async with database.sessions() as session:
        ticket = await ScopedRepo(session, Ticket, workspace_id).get(ticket_id)
        assert ticket is not None
        runs = ScopedRepo(session, Run, workspace_id)
        run = await runs.create(
            ticket_id=ticket_id,
            repo_id=ticket.repo_id,
            budget_usd=Decimal("5.0"),
            status=RunStatus.COMPLETED.value,
        )
        usages = ScopedRepo(session, LlmUsage, workspace_id)
        await usages.create(
            run_id=run.id,
            provider="test",
            model="test-model",
            input_tokens=0,
            output_tokens=0,
            cache_read_tokens=0,
            cache_creation_tokens=0,
            cost_usd=cost_usd,
        )
        await session.commit()


async def test_start_run_refuses_when_daily_budget_is_spent(
    run_api: Api, database: Database
) -> None:
    ticket_id = run_api.app.state.extra_test_ticket_id
    await _seed_completed_run_with_usage(
        database, run_api, ticket_id=ticket_id, cost_usd=Decimal("20.0")
    )

    response = await run_api.client.post(
        "/api/v1/runs", json={"ticket_id": ticket_id}, headers=run_api.auth
    )

    assert response.status_code == 409
    body = response.json()
    assert body["error"]["code"] == "daily_budget_exhausted"
    assert "20.0" in body["error"]["message"]


async def test_start_run_allows_when_daily_budget_is_not_spent(
    run_api: Api, database: Database
) -> None:
    ticket_id = run_api.app.state.extra_test_ticket_id
    await _seed_completed_run_with_usage(
        database, run_api, ticket_id=ticket_id, cost_usd=Decimal("19.999999")
    )

    response = await run_api.client.post(
        "/api/v1/runs", json={"ticket_id": ticket_id}, headers=run_api.auth
    )

    assert response.status_code == 201
    assert response.json()["ticket_id"] == ticket_id


async def test_approving_after_the_cap_was_raised_applies_the_pending_approval(
    database: Any, tmp_path: Path
) -> None:
    """Lead review 18a, B1: the resume re-run answers the pending row even when
    the cap no longer applies, instead of leaving it pending forever."""
    frozen = datetime(2026, 11, 14, 12, 0, tzinfo=UTC)
    async with guard_env(database, tmp_path, budget_usd="100.0", cost_usd="0.0") as env:
        await _add_usage(env, cost_usd=Decimal("20.0"), created_at=frozen)
        calls: list[Any] = []

        async def node(state: Any) -> dict[str, Any]:
            calls.append(state)
            return {"called": True}

        class Paused(Exception):
            pass

        def pause(payload: Any) -> Any:
            raise Paused

        monkeypatch = pytest.MonkeyPatch()
        monkeypatch.setattr("gaia.orchestrator.guard.interrupt", pause)
        with pytest.raises(Paused):
            await with_guard(env.deps, "node", now=lambda: frozen)(node)(state_for(env))

        # The owner raises the daily cap (a restart with a new setting), then approves.
        raised = replace(
            env.deps, settings=env.deps.settings.model_copy(update={"daily_budget_usd": 100.0})
        )
        monkeypatch.setattr(
            "gaia.orchestrator.guard.interrupt", lambda payload: {"action": "approve"}
        )
        result = await with_guard(raised, "node", now=lambda: frozen)(node)(state_for(env))
        monkeypatch.undo()

        assert result == {"called": True}
        assert len(calls) == 1
        async with session_scope(env.deps.sessions) as session:
            rows = await ScopedRepo(session, Approval, env.workspace_id).list(
                run_id=env.run_id, kind=ApprovalKind.BUDGET.value
            )
            assert [(row.idempotency_key, row.status) for row in rows] == [
                ("budget:node:daily:0", ApprovalStatus.APPROVED.value)
            ]
