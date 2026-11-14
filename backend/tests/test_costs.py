"""Cost accounting and the database usage recorder (doc 07)."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from gaia.config.settings import Settings
from gaia.db.models import LlmUsage as LlmUsageRow
from gaia.db.models import Run
from gaia.db.repo import ScopedRepo
from gaia.llm.base import LLMUsage
from gaia.services.costs import (
    BudgetExceeded,
    DbUsageRecorder,
    ModelPrice,
    PriceQuote,
    PriceRates,
    PriceTable,
    ensure_budget,
    load_price_table,
    remaining_budget,
)
from tests.db_builders import make_chain

UTC_NOON = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
UTC_MIDNIGHT = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)


def _usage(**overrides: int | str | None) -> LLMUsage:
    values: dict[str, int | str | None] = {
        "provider": "deepseek",
        "model": "deepseek-v4-pro",
        "input_tokens": 1_000_000,
        "output_tokens": 2_000_000,
        "cache_read_tokens": 500_000,
        "cache_creation_tokens": 0,
    }
    values.update(overrides)
    return LLMUsage(**values)  # type: ignore[arg-type]


def _table() -> PriceTable:
    return PriceTable(
        {
            "deepseek-v4-pro": ModelPrice(
                rates=PriceRates(
                    input=Decimal("1.0"),
                    output=Decimal("2.0"),
                    cache_read=Decimal("0.5"),
                ),
                off_peak=PriceRates(
                    input=Decimal("0.5"),
                    output=Decimal("1.0"),
                    cache_read=Decimal("0.25"),
                ),
                off_peak_start_utc=0,
                off_peak_end_utc=12,
            ),
        }
    )


def test_cost_uses_all_four_anthropic_buckets() -> None:
    table = PriceTable(
        {
            "claude-opus-5-5": ModelPrice(
                rates=PriceRates(
                    input=Decimal("4"),
                    output=Decimal("20"),
                    cache_read=Decimal("0.2"),
                    cache_write=Decimal("5"),
                )
            )
        }
    )
    usage = LLMUsage(
        provider="anthropic",
        model="claude-opus-5-5",
        input_tokens=1_000_000,
        output_tokens=500_000,
        cache_read_tokens=1_000_000,
        cache_creation_tokens=1_000_000,
    )
    assert table.cost(usage, at=UTC_NOON) == Decimal("19.2")
    quote = table.quote(usage, at=UTC_NOON)
    assert quote == PriceQuote(cost_usd=Decimal("19.2"), price_tier=None)


def test_deepseek_peak_and_off_peak_times_are_priced_differently() -> None:
    table = _table()
    usage = _usage()
    peak = table.quote(usage, at=UTC_NOON)
    off_peak = table.quote(usage, at=UTC_MIDNIGHT)
    assert peak.price_tier == "peak"
    assert off_peak.price_tier == "off-peak"
    assert peak.cost_usd == Decimal("5.25")
    assert off_peak.cost_usd == Decimal("2.625")


def test_off_peak_window_can_wrap_across_midnight() -> None:
    table = PriceTable(
        {
            "deepseek-v4-pro": ModelPrice(
                rates=PriceRates(input=Decimal("1"), output=Decimal("1")),
                off_peak=PriceRates(input=Decimal("0.5"), output=Decimal("0.5")),
                off_peak_start_utc=22,
                off_peak_end_utc=6,
            )
        }
    )
    usage = _usage(input_tokens=1_000_000, output_tokens=0, cache_read_tokens=0)
    assert table.quote(usage, at=datetime(2026, 1, 1, 23, 0, tzinfo=UTC)).price_tier == "off-peak"
    assert table.quote(usage, at=datetime(2026, 1, 1, 3, 0, tzinfo=UTC)).price_tier == "off-peak"
    assert table.quote(usage, at=UTC_NOON).price_tier == "peak"


def test_equal_start_and_end_disables_off_peak() -> None:
    table = PriceTable(
        {
            "m": ModelPrice(
                rates=PriceRates(input=Decimal("1"), output=Decimal("1")),
                off_peak=PriceRates(input=Decimal("0.5"), output=Decimal("0.5")),
                off_peak_start_utc=0,
                off_peak_end_utc=0,
            )
        }
    )
    usage = LLMUsage(provider="x", model="m", input_tokens=1_000_000)
    assert table.quote(usage, at=UTC_MIDNIGHT).price_tier == "peak"


def test_unknown_model_raises_a_key_error() -> None:
    with pytest.raises(KeyError, match="no price configured"):
        PriceTable({}).cost(LLMUsage(provider="x", model="nope"), at=UTC_NOON)


def test_load_price_table_reads_toml(tmp_path: Path) -> None:
    path = tmp_path / "prices.toml"
    path.write_text(
        """
[models.flat]
input = 1.0
output = 2.0

[models.tiered]
input = 1.0
output = 2.0
cache_read = 0.1
cache_write = 0.2
off_peak_start_utc = 1
off_peak_end_utc = 9

[models.tiered.off_peak]
input = 0.5
output = 1.0
cache_read = 0.05
cache_write = 0.1
""",
        encoding="utf-8",
    )
    table = load_price_table(path)
    assert table.model_names() == ("flat", "tiered")
    usage = LLMUsage(provider="x", model="flat", input_tokens=1_000_000, output_tokens=1_000_000)
    assert table.cost(usage, at=UTC_NOON) == Decimal("3")
    tiered = LLMUsage(provider="x", model="tiered", input_tokens=1_000_000)
    assert table.quote(tiered, at=datetime(2026, 1, 1, 3, 0, tzinfo=UTC)).price_tier == "off-peak"


def test_load_price_table_rejects_negative_price(tmp_path: Path) -> None:
    path = tmp_path / "bad.toml"
    path.write_text("[models.m]\ninput = -1\noutput = 0\n", encoding="utf-8")
    with pytest.raises(ValueError, match="must not be negative"):
        load_price_table(path)


def test_load_price_table_rejects_non_numeric_price(tmp_path: Path) -> None:
    path = tmp_path / "bad.toml"
    path.write_text('[models.m]\ninput = "cheap"\noutput = 0\n', encoding="utf-8")
    with pytest.raises(ValueError, match="must be a number"):
        load_price_table(path)


async def test_remaining_budget_subtracts_spend(session: AsyncSession) -> None:
    chain = await make_chain(session, slug="budget")
    runs = ScopedRepo(session, Run, chain.workspace.id)
    run = await runs.update(chain.run.id, budget_usd=Decimal("5.0"), cost_usd=Decimal("1.25"))
    assert remaining_budget(run) == Decimal("3.75")


async def test_db_usage_recorder_writes_through_scoped_repo(session: AsyncSession) -> None:
    chain = await make_chain(session, slug="usage")
    recorder = DbUsageRecorder(session, chain.workspace.id, chain.run.id)
    await recorder.record(
        LLMUsage(
            provider="deepseek",
            model="deepseek-flash",
            input_tokens=10,
            output_tokens=20,
            cache_read_tokens=3,
            request_id="req_ds",
        ),
        cost_usd=Decimal("0.123456"),
        price_tier="peak",
    )
    rows = await ScopedRepo(session, LlmUsageRow, chain.workspace.id).list()
    assert len(rows) == 1
    row = rows[0]
    assert row.run_id == chain.run.id
    assert row.run_step_id is None
    assert row.agent_id is None
    assert row.provider == "deepseek"
    assert row.model == "deepseek-flash"
    assert row.input_tokens == 10
    assert row.output_tokens == 20
    assert row.cache_read_tokens == 3
    assert row.cache_creation_tokens == 0
    assert row.price_tier == "peak"
    assert row.cost_usd == Decimal("0.123456")


def test_naive_datetime_is_treated_as_utc() -> None:
    table = _table()
    usage = _usage()
    naive_midnight = datetime(2026, 1, 1, 0, 0)
    assert table.quote(usage, at=naive_midnight).price_tier == "off-peak"
    assert table.quote(usage, at=naive_midnight + timedelta(hours=12)).price_tier == "peak"


async def test_recorder_adds_cost_to_the_run_atomically(session: AsyncSession) -> None:
    """The usage row and ``run.cost_usd = run.cost_usd + :c`` commit together."""
    chain = await make_chain(session, slug="recorder-cost")
    runs = ScopedRepo(session, Run, chain.workspace.id)
    await runs.update(chain.run.id, budget_usd=Decimal("5.0"))

    recorder = DbUsageRecorder(
        session,
        chain.workspace.id,
        chain.run.id,
        run_step_id=chain.step.id,
        agent_id=chain.agent.id,
    )
    await recorder.record(
        LLMUsage(
            provider="deepseek",
            model="deepseek-v4-pro",
            input_tokens=10,
            output_tokens=20,
            request_id="req_cost",
        ),
        cost_usd=Decimal("0.000040"),
        price_tier="peak",
    )
    session.expunge_all()

    run = await runs.get(chain.run.id)
    assert run is not None
    assert run.cost_usd == Decimal("0.000040")
    rows = await ScopedRepo(session, LlmUsageRow, chain.workspace.id).list()
    assert rows[0].run_step_id == chain.step.id
    assert rows[0].agent_id == chain.agent.id


async def test_ensure_budget_raises_for_a_spent_run(session: AsyncSession) -> None:
    chain = await make_chain(session, slug="budget-run")
    runs = ScopedRepo(session, Run, chain.workspace.id)
    run = await runs.update(chain.run.id, budget_usd=Decimal("5.0"), cost_usd=Decimal("5.0"))
    settings = Settings(data_dir=Path("/tmp/gaia-budget"), db_url="sqlite+aiosqlite:///:memory:")

    with pytest.raises(BudgetExceeded) as excinfo:
        await ensure_budget(session, run, settings, now=UTC_NOON)

    assert excinfo.value.kind == "run"
    assert excinfo.value.limit_usd == Decimal("5.0")
    assert excinfo.value.spent_usd == Decimal("5.0")


async def test_ensure_budget_raises_for_a_spent_utc_day(session: AsyncSession) -> None:
    chain = await make_chain(session, slug="budget-daily")
    runs = ScopedRepo(session, Run, chain.workspace.id)
    run = await runs.update(chain.run.id, budget_usd=Decimal("100.0"))
    usage_repo = ScopedRepo(session, LlmUsageRow, chain.workspace.id)
    await usage_repo.create(
        run_id=chain.run.id,
        provider="deepseek",
        model="deepseek-v4-pro",
        cost_usd=Decimal("20.0"),
    )
    settings = Settings(
        data_dir=Path("/tmp/gaia-budget"),
        db_url="sqlite+aiosqlite:///:memory:",
        daily_budget_usd=20.0,
    )

    with pytest.raises(BudgetExceeded) as excinfo:
        await ensure_budget(session, run, settings, now=datetime.now(UTC))

    assert excinfo.value.kind == "daily"
    assert excinfo.value.limit_usd == Decimal("20.0")
    assert excinfo.value.spent_usd == Decimal("20.0")


async def test_ensure_budget_allows_an_open_budget(session: AsyncSession) -> None:
    chain = await make_chain(session, slug="budget-open")
    runs = ScopedRepo(session, Run, chain.workspace.id)
    run = await runs.update(chain.run.id, budget_usd=Decimal("5.0"))
    settings = Settings(
        data_dir=Path("/tmp/gaia-budget"),
        db_url="sqlite+aiosqlite:///:memory:",
        daily_budget_usd=20.0,
    )

    await ensure_budget(session, run, settings, now=UTC_NOON)


def _windowed_table() -> PriceTable:
    return PriceTable(
        {
            "ds": ModelPrice(
                rates=PriceRates(input=Decimal("1.32"), output=Decimal("3.96")),
                off_peak=PriceRates(input=Decimal("0.66"), output=Decimal("1.98")),
                peak_windows_utc=((1, 4), (6, 10)),
                peak_weekdays_only=True,
            )
        }
    )


@pytest.mark.parametrize(
    ("at", "tier"),
    [
        (datetime(2026, 11, 16, 1, 0, tzinfo=UTC), "peak"),  # Monday 01:00
        (datetime(2026, 11, 16, 3, 59, tzinfo=UTC), "peak"),
        (datetime(2026, 11, 16, 4, 0, tzinfo=UTC), "off-peak"),  # gap between windows
        (datetime(2026, 11, 16, 9, 30, tzinfo=UTC), "peak"),
        (datetime(2026, 11, 16, 10, 0, tzinfo=UTC), "off-peak"),
        (datetime(2026, 11, 16, 0, 59, tzinfo=UTC), "off-peak"),
        (datetime(2026, 11, 14, 2, 0, tzinfo=UTC), "off-peak"),  # Saturday
    ],
)
def test_peak_windows_price_deepseeks_published_schedule(at: datetime, tier: str) -> None:
    """Lead review 07b (G6): DeepSeek peak is two weekday windows, off-peak is half."""
    usage = LLMUsage(provider="deepseek", model="ds", input_tokens=1_000_000)

    quote = _windowed_table().quote(usage, at=at)

    assert quote.price_tier == tier
    assert quote.cost_usd == (Decimal("1.32") if tier == "peak" else Decimal("0.66"))


@pytest.mark.parametrize("bad", [[[4, 1]], [[1]], [[1, 25]], ["1-4"]])
def test_peak_windows_are_validated(tmp_path: Path, bad: object) -> None:
    path = tmp_path / "prices.toml"
    path.write_text(
        f"[models.ds]\ninput = 1\noutput = 1\npeak_windows_utc = {json.dumps(bad)}\n"
        "[models.ds.off_peak]\ninput = 1\noutput = 1\n"
    )

    with pytest.raises(ValueError, match="peak_windows_utc"):
        load_price_table(path)


def test_the_shipped_price_table_has_verified_deepseek_rates() -> None:
    table = load_price_table()
    usage = LLMUsage(provider="deepseek", model="deepseek-v4-pro", cache_read_tokens=1_000_000)

    weekday_peak = table.quote(usage, at=datetime(2026, 11, 16, 2, 0, tzinfo=UTC))
    weekend = table.quote(usage, at=datetime(2026, 11, 15, 2, 0, tzinfo=UTC))

    assert weekday_peak.cost_usd == Decimal("0.044")
    assert weekend.cost_usd == Decimal("0.022")
