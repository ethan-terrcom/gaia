"""Today's LLM spend for the top bar (doc 16)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from fastapi import APIRouter

from gaia.api.deps import ScopedDep, ServicesDep
from gaia.db.repo import daily_llm_usage_by_provider, daily_llm_usage_cost_usd
from gaia.db.types import utcnow
from gaia.domain.schemas import ProviderCost, TodayCosts

router = APIRouter(tags=["costs"])


def _now() -> datetime:
    """Return the current aware UTC time.

    A function rather than a bare ``utcnow`` import so tests can patch the clock
    without touching the database column helper.
    """
    return utcnow()


@router.get("/costs/today", response_model=TodayCosts, summary="Today's LLM spend")
async def get_today_costs(scoped: ScopedDep, services: ServicesDep) -> TodayCosts:
    """Return the UTC day's spend, budget and per-provider breakdown."""
    now = _now()
    day_start = now.astimezone(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    day_end = day_start + timedelta(days=1)
    spent = await daily_llm_usage_cost_usd(
        scoped.session,
        scoped.workspace_id,
        start=day_start,
        end=day_end,
    )
    by_provider = await daily_llm_usage_by_provider(
        scoped.session,
        scoped.workspace_id,
        start=day_start,
        end=day_end,
    )
    result = TodayCosts(
        spent_usd=spent,
        budget_usd=Decimal(str(services.settings.daily_budget_usd)),
        by_provider=[
            ProviderCost(provider=provider, cost_usd=cost) for provider, cost in by_provider
        ],
        day_start=day_start,
    )
    return result
