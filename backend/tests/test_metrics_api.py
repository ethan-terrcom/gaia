"""Metrics summary endpoint (doc 18b)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from gaia.api.deps import Scoped
from gaia.api.routes import metrics as metrics_module
from gaia.db.models import Approval, LlmUsage, Repository, Run, Ticket
from gaia.db.repo import (
    ScopedRepo,
    _percentile,
    approval_wait_stats,
    finished_run_average_cost_usd,
)
from gaia.domain.enums import ApprovalKind, ApprovalStatus, RunStatus
from tests.conftest import Api, Database
from tests.db_builders import make_chain

NOW = datetime(2026, 1, 15, 12, 0, tzinfo=UTC)


def _workspace_id(api: Api) -> str:
    workspace_id: str = api.app.state.services.principal.workspace_id
    return workspace_id


async def _make_run(
    api: Api,
    database: Database,
    *,
    name: str,
    status: RunStatus = RunStatus.PENDING,
    cost_usd: str = "0",
) -> str:
    """Create a repository/ticket/run in the API's seeded workspace."""
    workspace_id = _workspace_id(api)
    async with database.sessions() as session:
        repositories = ScopedRepo(session, Repository, workspace_id)
        repository = await repositories.create(name=name, local_path=f"/tmp/{name}")
        tickets = ScopedRepo(session, Ticket, workspace_id)
        ticket = await tickets.create(repo_id=repository.id, title=name)
        runs = ScopedRepo(session, Run, workspace_id)
        run = await runs.create(
            ticket_id=ticket.id,
            repo_id=repository.id,
            status=status.value,
            cost_usd=Decimal(cost_usd),
        )
        await session.commit()
    return run.id


async def _add_usage(
    api: Api,
    database: Database,
    run_id: str,
    *,
    provider: str,
    model: str,
    cost_usd: str,
    created_at: datetime,
) -> None:
    workspace_id = _workspace_id(api)
    async with database.sessions() as session:
        usage = ScopedRepo(session, LlmUsage, workspace_id)
        await usage.create(
            run_id=run_id,
            provider=provider,
            model=model,
            cost_usd=Decimal(cost_usd),
            created_at=created_at,
        )
        await session.commit()


async def _add_approval(
    api: Api,
    database: Database,
    run_id: str,
    *,
    idempotency_key: str,
    created_at: datetime,
    decided_after_s: int | None = None,
) -> None:
    workspace_id = _workspace_id(api)
    values: dict[str, object] = {
        "run_id": run_id,
        "idempotency_key": idempotency_key,
        "kind": ApprovalKind.PLAN.value,
        "payload_json": {},
        "status": (
            ApprovalStatus.APPROVED.value
            if decided_after_s is not None
            else ApprovalStatus.PENDING.value
        ),
        "created_at": created_at,
    }
    if decided_after_s is not None:
        values["decided_at"] = created_at + timedelta(seconds=decided_after_s)
    async with database.sessions() as session:
        approvals = ScopedRepo(session, Approval, workspace_id)
        await approvals.create(**values)
        await session.commit()


def test_metrics_clock_returns_aware_utc() -> None:
    now = metrics_module._now()

    assert now.tzinfo is not None


async def test_summary_route_function_directly(
    api: Api,
    database: Database,
    monkeypatch,
) -> None:
    """Call the route function directly, without FastAPI's dependency wrapping."""
    monkeypatch.setattr(metrics_module, "_now", lambda: NOW)
    workspace_id = _workspace_id(api)
    run_id = await _make_run(
        api,
        database,
        name="direct",
        status=RunStatus.COMPLETED,
        cost_usd="2.50",
    )
    await _add_usage(
        api,
        database,
        run_id,
        provider="deepseek",
        model="deepseek-v4-pro",
        cost_usd="2.50",
        created_at=datetime(2026, 1, 10, tzinfo=UTC),
    )
    await _add_approval(
        api,
        database,
        run_id,
        idempotency_key="direct",
        created_at=datetime(2026, 1, 10, tzinfo=UTC),
        decided_after_s=7,
    )

    async with database.sessions() as session:
        result = await metrics_module.get_metrics_summary(
            Scoped(session, workspace_id),
            days=14,
        )

    assert result.runs_by_status == {RunStatus.COMPLETED.value: 1}
    assert result.success_rate == 1.0
    assert result.avg_cost_per_run_usd == Decimal("2.50")
    assert result.cost_by_model[0].cost_usd == Decimal("2.50")
    assert result.approval_wait_s is not None
    assert result.approval_wait_s.count == 1
    assert result.approval_wait_s.avg == 7.0
    assert result.approval_wait_s.p50 == 7.0
    assert result.approval_wait_s.p90 == 7.0


async def test_finished_run_average_cost_usd_is_none_without_finished_runs(
    api: Api,
    database: Database,
) -> None:
    workspace_id = _workspace_id(api)

    async with database.sessions() as session:
        result = await finished_run_average_cost_usd(session, workspace_id)

    assert result is None


async def test_approval_wait_stats_returns_none_with_no_decided_approvals(
    api: Api,
    database: Database,
) -> None:
    workspace_id = _workspace_id(api)

    async with database.sessions() as session:
        result = await approval_wait_stats(
            session,
            workspace_id,
            start=datetime(2026, 1, 1, tzinfo=UTC),
            end=datetime(2026, 2, 1, tzinfo=UTC),
        )

    assert result is None


async def test_approval_wait_stats_directly(
    api: Api,
    database: Database,
) -> None:
    workspace_id = _workspace_id(api)
    run_id = await _make_run(api, database, name="wait-direct")
    await _add_approval(
        api,
        database,
        run_id,
        idempotency_key="direct-wait",
        created_at=datetime(2026, 1, 10, 12, 0, tzinfo=UTC),
        decided_after_s=20,
    )

    async with database.sessions() as session:
        result = await approval_wait_stats(
            session,
            workspace_id,
            start=datetime(2026, 1, 1, tzinfo=UTC),
            end=datetime(2026, 2, 1, tzinfo=UTC),
        )

    assert result is not None
    assert result.count == 1
    assert result.avg_s == 20.0
    assert result.p50_s == 20.0
    assert result.p90_s == 20.0


def test_percentile_rejects_an_empty_list() -> None:
    with pytest.raises(ValueError, match="empty"):
        _percentile([], 50)


async def test_summary_fields(api: Api, database: Database, monkeypatch) -> None:
    monkeypatch.setattr(metrics_module, "_now", lambda: NOW)
    completed_run = await _make_run(
        api, database, name="completed", status=RunStatus.COMPLETED, cost_usd="10"
    )
    failed_run = await _make_run(
        api, database, name="failed", status=RunStatus.FAILED, cost_usd="20"
    )
    cancelled_run = await _make_run(
        api, database, name="cancelled", status=RunStatus.CANCELLED, cost_usd="30"
    )
    await _make_run(api, database, name="pending", status=RunStatus.PENDING, cost_usd="100")

    await _add_usage(
        api,
        database,
        completed_run,
        provider="deepseek",
        model="deepseek-v4-pro",
        cost_usd="1.25",
        created_at=datetime(2026, 1, 10, 10, 0, tzinfo=UTC),
    )
    await _add_usage(
        api,
        database,
        completed_run,
        provider="deepseek",
        model="deepseek-v4-pro",
        cost_usd="2.00",
        created_at=datetime(2026, 1, 11, 12, 0, tzinfo=UTC),
    )
    await _add_usage(
        api,
        database,
        failed_run,
        provider="anthropic",
        model="claude-opus-5-5",
        cost_usd="0.75",
        created_at=datetime(2026, 1, 10, 11, 0, tzinfo=UTC),
    )
    await _add_usage(
        api,
        database,
        failed_run,
        provider="deepseek",
        model="deepseek-v4-pro",
        cost_usd="50.00",
        created_at=datetime(2026, 1, 1, 23, 59, tzinfo=UTC),
    )

    await _add_approval(
        api,
        database,
        completed_run,
        idempotency_key="fast",
        created_at=datetime(2026, 1, 10, tzinfo=UTC),
        decided_after_s=10,
    )
    await _add_approval(
        api,
        database,
        failed_run,
        idempotency_key="slow",
        created_at=datetime(2026, 1, 11, tzinfo=UTC),
        decided_after_s=30,
    )
    await _add_approval(
        api,
        database,
        cancelled_run,
        idempotency_key="pending",
        created_at=datetime(2026, 1, 12, tzinfo=UTC),
    )

    response = await api.client.get(
        "/api/v1/metrics/summary",
        params={"days": 14},
        headers=api.auth,
    )

    assert response.status_code == 200
    body = response.json()
    assert body["runs_by_status"] == {
        RunStatus.COMPLETED.value: 1,
        RunStatus.FAILED.value: 1,
        RunStatus.CANCELLED.value: 1,
        RunStatus.PENDING.value: 1,
    }
    assert body["success_rate"] == 1 / 3
    assert Decimal(body["avg_cost_per_run_usd"]) == Decimal("20.0")
    assert [
        (entry["provider"], entry["model"], Decimal(entry["cost_usd"]))
        for entry in body["cost_by_model"]
    ] == [
        ("anthropic", "claude-opus-5-5", Decimal("0.75")),
        ("deepseek", "deepseek-v4-pro", Decimal("3.25")),
    ]
    assert [(entry["day"], Decimal(entry["cost_usd"])) for entry in body["cost_by_day"]] == [
        ("2026-01-10", Decimal("2.00")),
        ("2026-01-11", Decimal("2.00")),
    ]
    assert body["approval_wait_s"] == {
        "count": 2,
        "avg": 20.0,
        "p50": 10.0,
        "p90": 30.0,
    }


async def test_metrics_excludes_outside_window_and_other_workspace(
    api: Api,
    database: Database,
    monkeypatch,
) -> None:
    monkeypatch.setattr(metrics_module, "_now", lambda: NOW)
    local_run = await _make_run(
        api,
        database,
        name="local",
        status=RunStatus.COMPLETED,
        cost_usd="5",
    )
    await _add_usage(
        api,
        database,
        local_run,
        provider="deepseek",
        model="deepseek-v4-pro",
        cost_usd="1.00",
        created_at=datetime(2026, 1, 10, tzinfo=UTC),
    )
    await _add_usage(
        api,
        database,
        local_run,
        provider="deepseek",
        model="deepseek-v4-pro",
        cost_usd="99.00",
        created_at=datetime(2025, 12, 31, tzinfo=UTC),
    )

    async with database.sessions() as session:
        foreign = await make_chain(session, slug="foreign-metrics")
        foreign_usage = ScopedRepo(session, LlmUsage, foreign.workspace.id)
        await foreign_usage.create(
            run_id=foreign.run.id,
            provider="anthropic",
            model="claude-opus-5-5",
            cost_usd=Decimal("50.00"),
            created_at=datetime(2026, 1, 10, tzinfo=UTC),
        )
        await session.commit()

    response = await api.client.get(
        "/api/v1/metrics/summary",
        params={"days": 14},
        headers=api.auth,
    )

    assert response.status_code == 200
    body = response.json()
    assert body["runs_by_status"] == {RunStatus.COMPLETED.value: 1}
    assert Decimal(body["avg_cost_per_run_usd"]) == Decimal("5.0")
    assert [
        (entry["provider"], entry["model"], Decimal(entry["cost_usd"]))
        for entry in body["cost_by_model"]
    ] == [("deepseek", "deepseek-v4-pro", Decimal("1.00"))]
    assert [(entry["day"], Decimal(entry["cost_usd"])) for entry in body["cost_by_day"]] == [
        ("2026-01-10", Decimal("1.00"))
    ]


async def test_empty_database_returns_zeros_or_none(
    api: Api,
    monkeypatch,
) -> None:
    monkeypatch.setattr(metrics_module, "_now", lambda: NOW)

    response = await api.client.get(
        "/api/v1/metrics/summary",
        params={"days": 14},
        headers=api.auth,
    )

    assert response.status_code == 200
    body = response.json()
    assert body["runs_by_status"] == {}
    assert body["success_rate"] is None
    assert body["avg_cost_per_run_usd"] is None
    assert body["cost_by_model"] == []
    assert body["cost_by_day"] == []
    assert body["approval_wait_s"] is None


async def test_days_query_parameter_is_bounded(api: Api) -> None:
    too_low = await api.client.get(
        "/api/v1/metrics/summary",
        params={"days": 0},
        headers=api.auth,
    )
    too_high = await api.client.get(
        "/api/v1/metrics/summary",
        params={"days": 91},
        headers=api.auth,
    )

    assert too_low.status_code == 422
    assert too_high.status_code == 422
