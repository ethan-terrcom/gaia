"""Cost accounting and the database usage recorder (doc 07)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from gaia.db.models import LlmUsage as LlmUsageRow
from gaia.db.models import Run
from gaia.db.repo import ScopedRepo
from gaia.llm.base import LLMUsage
from gaia.services.costs import (
    DbUsageRecorder,
    ModelPrice,
    PriceQuote,
    PriceRates,
    PriceTable,
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
