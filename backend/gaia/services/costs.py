"""Cost accounting: price tables and the database usage recorder (doc 07).

Prices live in ``gaia/llm/prices.toml`` so the owner can correct them without a
code change. ``PriceTable`` is deliberately separate from the provider clients:
a test (or 07b's gateway) can pass a custom table and never touch the shipped
file.
"""

from __future__ import annotations

import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from gaia.config.settings import Settings
from gaia.db.models import LlmUsage as LlmUsageRow
from gaia.db.models import Run
from gaia.db.repo import ScopedRepo, add_run_cost, daily_llm_usage_cost_usd
from gaia.llm.base import LLMUsage

#: Shipped price table. Tests build their own tables and never read this file.
DEFAULT_PRICE_TABLE_PATH = Path(__file__).resolve().parent.parent / "llm" / "prices.toml"

logger = structlog.get_logger(__name__)

#: ``price_tier`` recorded when the serving model had no price of its own.
PRICED_AS_REQUESTED = "priced-as-requested"

#: Tokens per billing unit in ``prices.toml``.
TOKENS_PER_BILLING_UNIT = Decimal(1_000_000)


@dataclass(frozen=True)
class PriceRates:
    """USD per 1M tokens for the four common billing buckets."""

    input: Decimal
    output: Decimal
    cache_read: Decimal = Decimal(0)
    cache_write: Decimal = Decimal(0)


@dataclass(frozen=True)
class ModelPrice:
    """Rates for one model, plus an optional off-peak tier."""

    rates: PriceRates
    off_peak: PriceRates | None = None
    off_peak_start_utc: int | None = None
    off_peak_end_utc: int | None = None
    #: Peak hours as half-open UTC ``(start, end)`` pairs. When set, the
    #: off-peak rates apply at every *other* time, and the ``off_peak_*_utc``
    #: single window is ignored. DeepSeek publishes its schedule this way.
    peak_windows_utc: tuple[tuple[int, int], ...] = ()
    #: With ``peak_windows_utc``: peak hours apply Monday to Friday only.
    peak_weekdays_only: bool = False


@dataclass(frozen=True)
class PriceQuote:
    """The cost for one usage object and the tier that was applied."""

    cost_usd: Decimal
    price_tier: str | None


def _decimal(value: Any, *, field: str, model: str) -> Decimal:
    """Read a non-negative decimal from TOML, with a useful error message."""
    try:
        result = Decimal(str(value))
    except (ValueError, TypeError, InvalidOperation) as exc:
        raise ValueError(f"{model}.{field} must be a number, got {value!r}") from exc
    if result < 0:
        raise ValueError(f"{model}.{field} must not be negative")
    return result


def _parse_model(name: str, raw: Mapping[str, Any]) -> ModelPrice:
    """Parse one ``[models.<name>]`` section from ``prices.toml``."""
    rates = PriceRates(
        input=_decimal(raw["input"], field="input", model=name),
        output=_decimal(raw["output"], field="output", model=name),
        cache_read=_decimal(raw.get("cache_read", 0), field="cache_read", model=name),
        cache_write=_decimal(raw.get("cache_write", 0), field="cache_write", model=name),
    )

    raw_off_peak = raw.get("off_peak")
    if raw_off_peak is None:
        return ModelPrice(rates=rates)

    return ModelPrice(
        rates=rates,
        off_peak=PriceRates(
            input=_decimal(raw_off_peak["input"], field="off_peak.input", model=name),
            output=_decimal(raw_off_peak["output"], field="off_peak.output", model=name),
            cache_read=_decimal(
                raw_off_peak.get("cache_read", 0), field="off_peak.cache_read", model=name
            ),
            cache_write=_decimal(
                raw_off_peak.get("cache_write", 0), field="off_peak.cache_write", model=name
            ),
        ),
        off_peak_start_utc=int(raw.get("off_peak_start_utc", 0)),
        off_peak_end_utc=int(raw.get("off_peak_end_utc", 0)),
        peak_windows_utc=_parse_windows(name, raw.get("peak_windows_utc", [])),
        peak_weekdays_only=bool(raw.get("peak_weekdays_only", False)),
    )


def _parse_windows(model: str, raw: Any) -> tuple[tuple[int, int], ...]:
    """Parse ``peak_windows_utc = [[1, 4], [6, 10]]`` into checked hour pairs."""
    windows: list[tuple[int, int]] = []
    for window in raw:
        if (
            not isinstance(window, list)
            or len(window) != 2
            or not all(isinstance(hour, int) and 0 <= hour <= 24 for hour in window)
            or window[0] >= window[1]
        ):
            raise ValueError(f"{model}.peak_windows_utc entries must be [start, end] hours")
        windows.append((window[0], window[1]))
    return tuple(windows)


def load_price_table(path: Path = DEFAULT_PRICE_TABLE_PATH) -> PriceTable:
    """Load a price table from TOML."""
    with path.open("rb") as price_file:
        data = tomllib.load(price_file)
    models_raw = data.get("models", {})
    models = {str(name): _parse_model(str(name), raw) for name, raw in models_raw.items()}
    return PriceTable(models)


def _as_utc(at: datetime) -> datetime:
    """Return ``at`` as aware UTC, treating a naive value as UTC."""
    return at.replace(tzinfo=UTC) if at.tzinfo is None else at.astimezone(UTC)


def _is_off_peak(at: datetime, start: int, end: int) -> bool:
    """Return whether ``at`` falls inside a UTC half-open ``[start, end)`` window.

    ``start == end`` means "off-peak disabled"; a window that crosses midnight is
    expressed with ``start > end``.
    """
    hour = _as_utc(at).hour
    if start == end:
        return False
    if start < end:
        return start <= hour < end
    return hour >= start or hour < end


def _is_peak(at: datetime, windows: tuple[tuple[int, int], ...], weekdays_only: bool) -> bool:
    """Return whether ``at`` falls in one of the provider's peak windows."""
    moment = _as_utc(at)
    if weekdays_only and moment.weekday() >= 5:
        return False
    return any(start <= moment.hour < end for start, end in windows)


class PriceTable:
    """Per-model prices and the formula that turns tokens into USD."""

    def __init__(self, models: Mapping[str, ModelPrice]) -> None:
        self._models = dict(models)

    def quote(self, usage: LLMUsage, *, at: datetime) -> PriceQuote:
        """Price one usage object at ``at`` and say which tier applied."""
        try:
            price = self._models[usage.model]
        except KeyError as exc:
            raise KeyError(f"no price configured for model {usage.model!r}") from exc

        tier: str | None = None
        rates = price.rates
        if price.off_peak is not None and price.peak_windows_utc:
            if _is_peak(at, price.peak_windows_utc, price.peak_weekdays_only):
                tier = "peak"
            else:
                rates = price.off_peak
                tier = "off-peak"
        elif price.off_peak is not None:
            if _is_off_peak(
                at,
                price.off_peak_start_utc if price.off_peak_start_utc is not None else 0,
                price.off_peak_end_utc if price.off_peak_end_utc is not None else 0,
            ):
                rates = price.off_peak
                tier = "off-peak"
            else:
                tier = "peak"

        cost = (
            Decimal(usage.input_tokens) * rates.input
            + Decimal(usage.output_tokens) * rates.output
            + Decimal(usage.cache_read_tokens) * rates.cache_read
            + Decimal(usage.cache_creation_tokens) * rates.cache_write
        ) / TOKENS_PER_BILLING_UNIT
        return PriceQuote(cost_usd=cost, price_tier=tier)

    def require(self, model: str) -> None:
        """Fail fast, before any money is spent, when ``model`` has no price."""
        if model not in self._models:
            raise KeyError(f"no price configured for model {model!r}; add it to prices.toml")

    def quote_or_price_as(
        self, usage: LLMUsage, *, at: datetime, fallback_model: str
    ) -> PriceQuote:
        """Price ``usage``; if its model is unknown, price it as ``fallback_model``.

        A refusal fallback (doc 07) or a provider alias can report a serving
        model that ``prices.toml`` does not list. The call has already been
        billed, so losing its usage row would be worse than an approximate
        price: the row keeps the real model, the tier says it was estimated.
        """
        if usage.model in self._models:
            return self.quote(usage, at=at)
        logger.warning("llm.unpriced_model", model=usage.model, priced_as=fallback_model)
        estimate = self.quote(replace(usage, model=fallback_model), at=at)
        return PriceQuote(cost_usd=estimate.cost_usd, price_tier=PRICED_AS_REQUESTED)

    def cost(self, usage: LLMUsage, *, at: datetime) -> Decimal:
        """Return only the USD cost for ``usage``."""
        return self.quote(usage, at=at).cost_usd

    def model_names(self) -> tuple[str, ...]:
        """All configured models, sorted for deterministic error messages."""
        return tuple(sorted(self._models))


class DbUsageRecorder:
    """Write ``llm_usage`` rows through :class:`ScopedRepo` (doc 07 task 4).

    The run/step/agent context is fixed when the recorder is constructed, so a
    provider client never has to know which run it is serving.
    """

    def __init__(
        self,
        session: AsyncSession,
        workspace_id: str,
        run_id: str,
        *,
        run_step_id: str | None = None,
        agent_id: str | None = None,
    ) -> None:
        self._repo = ScopedRepo(session, LlmUsageRow, workspace_id)
        self._run_id = run_id
        self._run_step_id = run_step_id
        self._agent_id = agent_id

    async def record(
        self,
        usage: LLMUsage,
        *,
        cost_usd: Decimal,
        price_tier: str | None = None,
    ) -> None:
        """Insert one usage row and add the cost to ``run.cost_usd``.

        Both writes happen in the caller's transaction. The run update is an
        atomic ``cost_usd = cost_usd + :c`` (doc 07b), never a read-modify-write.
        """
        await self._repo.create(
            run_id=self._run_id,
            run_step_id=self._run_step_id,
            agent_id=self._agent_id,
            provider=usage.provider,
            model=usage.model,
            request_id=usage.request_id,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cache_read_tokens=usage.cache_read_tokens,
            cache_creation_tokens=usage.cache_creation_tokens,
            price_tier=price_tier,
            cost_usd=cost_usd,
        )
        await add_run_cost(self._repo.session, self._run_id, cost_usd)


class BudgetExceeded(Exception):
    """A run or workspace daily budget is used up (doc 07b).

    ``kind`` is ``"run"`` or ``"daily"``. The message is built from the numbers
    but never from provider or request content.
    """

    def __init__(self, kind: str, limit_usd: Decimal, spent_usd: Decimal) -> None:
        self.kind = kind
        self.limit_usd = limit_usd
        self.spent_usd = spent_usd
        super().__init__(f"{kind} LLM budget exceeded: {spent_usd} of {limit_usd} USD spent")


async def ensure_budget(
    session: AsyncSession, run: Run, settings: Settings, *, now: datetime
) -> None:
    """Raise :class:`BudgetExceeded` before forwarding when no budget is left.

    The run cap is checked first and the UTC-day cap second, so a run that is
    individually within budget still stops when the workspace has spent its
    daily allowance.
    """
    if remaining_budget(run) <= 0:
        raise BudgetExceeded("run", run.budget_usd, run.cost_usd)

    day_start = _as_utc(now).replace(hour=0, minute=0, second=0, microsecond=0)
    spent_today = await daily_llm_usage_cost_usd(
        session, run.workspace_id, start=day_start, end=day_start + timedelta(days=1)
    )
    daily_limit = Decimal(str(settings.daily_budget_usd))
    if spent_today >= daily_limit:
        raise BudgetExceeded("daily", daily_limit, spent_today)


def remaining_budget(run: Run) -> Decimal:
    """Return the USD left on ``run``.

    Enforcement is :func:`ensure_budget`, which the gateway calls before it
    forwards a provider request.
    """
    return run.budget_usd - run.cost_usd
