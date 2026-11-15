"""Today's spend endpoint (doc 16)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from gaia.api.deps import Scoped
from gaia.api.routes import costs as costs_module
from gaia.db.models import LlmUsage as LlmUsageRow
from gaia.db.models import Repository, Run, Ticket
from gaia.db.repo import ScopedRepo
from tests.conftest import Api, Database

UTC_NOON = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)


def test_costs_clock_returns_aware_utc() -> None:
    now = costs_module._now()

    assert now.tzinfo is not None


async def _make_run(api: Api, database: Database, *, name: str) -> str:
    workspace_id = api.app.state.services.principal.workspace_id
    async with database.sessions() as session:
        repos = ScopedRepo(session, Repository, workspace_id)
        repository = await repos.create(name=name, local_path=f"/tmp/{name}")
        tickets = ScopedRepo(session, Ticket, workspace_id)
        ticket = await tickets.create(repo_id=repository.id, title=name)
        runs = ScopedRepo(session, Run, workspace_id)
        run = await runs.create(ticket_id=ticket.id, repo_id=repository.id)
        await session.commit()
    return run.id


async def _add_usage(
    api: Api,
    database: Database,
    run_id: str,
    *,
    provider: str,
    cost_usd: str,
    created_at: datetime,
) -> None:
    workspace_id = api.app.state.services.principal.workspace_id
    async with database.sessions() as session:
        usage = ScopedRepo(session, LlmUsageRow, workspace_id)
        await usage.create(
            run_id=run_id,
            provider=provider,
            model="deepseek-v4-pro",
            cost_usd=Decimal(cost_usd),
            created_at=created_at,
        )
        await session.commit()


async def test_today_costs_sums_the_utc_day_and_breaks_down_by_provider(
    api: Api,
    database: Database,
    monkeypatch,
) -> None:
    run_id = await _make_run(api, database, name="costs-today")
    monkeypatch.setattr(costs_module, "_now", lambda: UTC_NOON)
    await _add_usage(
        api,
        database,
        run_id,
        provider="deepseek",
        cost_usd="1.25",
        created_at=UTC_NOON - timedelta(hours=2),
    )
    await _add_usage(
        api,
        database,
        run_id,
        provider="deepseek",
        cost_usd="2.00",
        created_at=UTC_NOON - timedelta(minutes=1),
    )
    await _add_usage(
        api,
        database,
        run_id,
        provider="anthropic",
        cost_usd="0.50",
        created_at=UTC_NOON - timedelta(hours=1),
    )
    await _add_usage(
        api,
        database,
        run_id,
        provider="anthropic",
        cost_usd="9.00",
        created_at=UTC_NOON - timedelta(days=1),
    )

    response = await api.client.get("/api/v1/costs/today", headers=api.auth)

    assert response.status_code == 200
    body = response.json()
    assert Decimal(body["spent_usd"]) == Decimal("3.75")
    assert Decimal(body["budget_usd"]) == Decimal("20.0")
    assert [(entry["provider"], Decimal(entry["cost_usd"])) for entry in body["by_provider"]] == [
        ("anthropic", Decimal("0.50")),
        ("deepseek", Decimal("3.25")),
    ]
    assert body["day_start"].startswith("2026-01-01T00:00:00")


async def test_get_today_costs_function_directly(
    api: Api,
    database: Database,
    monkeypatch,
) -> None:
    run_id = await _make_run(api, database, name="costs-direct")
    monkeypatch.setattr(costs_module, "_now", lambda: UTC_NOON)
    await _add_usage(
        api,
        database,
        run_id,
        provider="deepseek",
        cost_usd="0.25",
        created_at=UTC_NOON,
    )

    workspace_id = api.app.state.services.principal.workspace_id
    async with database.sessions() as session:
        result = await costs_module.get_today_costs(
            Scoped(session, workspace_id),
            api.app.state.services,
        )

    assert result.spent_usd == Decimal("0.25")
    assert result.budget_usd == Decimal("20.0")
