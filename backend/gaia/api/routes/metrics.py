"""Workspace metrics for the observability dashboard (doc 18b)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Query

from gaia.api.deps import ScopedDep
from gaia.db.repo import (
    approval_wait_stats,
    finished_run_average_cost_usd,
    llm_usage_cost_by_day,
    llm_usage_cost_by_model,
    run_status_counts,
)
from gaia.db.types import utcnow
from gaia.domain.enums import RunStatus
from gaia.domain.schemas import (
    ApprovalWaitSummary,
    CostByDay,
    CostByModel,
    MetricsSummary,
)

router = APIRouter(tags=["metrics"])


def _now() -> datetime:
    """Return the current aware UTC time.

    A function rather than a bare ``utcnow`` import so tests can patch the clock
    without touching the database column helper.
    """
    return utcnow()


@router.get(
    "/metrics/summary",
    response_model=MetricsSummary,
    summary="Workspace metrics summary",
)
async def get_metrics_summary(
    scoped: ScopedDep,
    days: int = Query(default=14, ge=1, le=90),
) -> MetricsSummary:
    """Return runs, spend and approval wait metrics for the last UTC days.

    The window covers ``days`` UTC calendar days ending today (UTC), which keeps
    "last 1 day" aligned to midnight UTC rather than a rolling 24 hours.
    """
    now = _now()
    today_start = now.astimezone(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    start = today_start - timedelta(days=days - 1)
    end = today_start + timedelta(days=1)

    counts = await run_status_counts(scoped.session, scoped.workspace_id)
    average_cost = await finished_run_average_cost_usd(scoped.session, scoped.workspace_id)

    completed = counts.get(RunStatus.COMPLETED.value, 0)
    failed = counts.get(RunStatus.FAILED.value, 0)
    cancelled = counts.get(RunStatus.CANCELLED.value, 0)
    denominator = completed + failed + cancelled
    success_rate = completed / denominator if denominator else None

    cost_by_model = await llm_usage_cost_by_model(
        scoped.session,
        scoped.workspace_id,
        start=start,
        end=end,
    )
    cost_by_day = await llm_usage_cost_by_day(
        scoped.session,
        scoped.workspace_id,
        start=start,
        end=end,
    )
    wait_stats = await approval_wait_stats(
        scoped.session,
        scoped.workspace_id,
        start=start,
        end=end,
    )

    return MetricsSummary(
        runs_by_status=counts,
        success_rate=success_rate,
        avg_cost_per_run_usd=average_cost,
        cost_by_model=[
            CostByModel(provider=provider, model=model, cost_usd=cost)
            for provider, model, cost in cost_by_model
        ],
        cost_by_day=[CostByDay(day=day, cost_usd=cost) for day, cost in cost_by_day],
        approval_wait_s=(
            ApprovalWaitSummary(
                count=wait_stats.count,
                avg=wait_stats.avg_s,
                p50=wait_stats.p50_s,
                p90=wait_stats.p90_s,
            )
            if wait_stats is not None
            else None
        ),
    )
