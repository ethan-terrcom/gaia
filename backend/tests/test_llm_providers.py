"""Provider-neutral structured-LLM factory (ADR-001 P2)."""

from __future__ import annotations

from decimal import Decimal

from pydantic import SecretStr

from gaia.llm.anthropic_client import AnthropicClient
from gaia.llm.base import LLMUsage
from gaia.llm.openai_compat_client import DeepSeekClient
from gaia.llm.providers import build_structured_llm
from gaia.services.costs import ModelPrice, PriceRates, PriceTable


class RecordingRecorder:
    def __init__(self) -> None:
        self.records: list[tuple[LLMUsage, Decimal, str | None]] = []

    async def record(
        self,
        usage: LLMUsage,
        *,
        cost_usd: Decimal,
        price_tier: str | None = None,
    ) -> None:
        self.records.append((usage, cost_usd, price_tier))


def _price_table() -> PriceTable:
    rates = PriceRates(input=Decimal("1"), output=Decimal("2"))
    return PriceTable(
        {
            "claude-opus-5-5": ModelPrice(rates=rates),
            "deepseek-v4-pro": ModelPrice(rates=rates),
        }
    )


def test_build_structured_llm_dispatches_to_anthropic_client() -> None:
    client = build_structured_llm(
        provider="anthropic",
        model="claude-opus-5-5",
        api_key=SecretStr("sk-ant-test-1234567890"),
        base_url="http://127.0.0.1:8765/llm/anthropic",
        recorder=RecordingRecorder(),
        price_table=_price_table(),
    )

    assert isinstance(client, AnthropicClient)


def test_build_structured_llm_dispatches_to_deepseek_client() -> None:
    client = build_structured_llm(
        provider="deepseek",
        model="deepseek-v4-pro",
        api_key=SecretStr("sk-deepseek-test-1234567890"),
        base_url="http://127.0.0.1:8765/llm/deepseek",
        recorder=RecordingRecorder(),
        price_table=_price_table(),
    )

    assert isinstance(client, DeepSeekClient)
