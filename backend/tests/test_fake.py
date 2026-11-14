"""FakeLLM: scripted responses and optional recording (doc 07 task 5)."""

from __future__ import annotations

from decimal import Decimal

import pytest

from gaia.llm.base import LLMMessage, LLMResponse, LLMUsage
from gaia.llm.fake import FakeLLM
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


def _response(text: str) -> LLMResponse:
    return LLMResponse(
        text=text,
        usage=LLMUsage(provider="fake", model="fake-model", output_tokens=3),
    )


def _table() -> PriceTable:
    return PriceTable(
        {"fake-model": ModelPrice(rates=PriceRates(input=Decimal("0"), output=Decimal("2")))}
    )


async def test_returns_scripted_responses_in_sequence() -> None:
    fake = FakeLLM([_response("one"), _response("two")])
    messages = [LLMMessage(role="user", content="go")]

    first = await fake.complete(messages=messages)
    second = await fake.complete(messages=messages)

    assert first.text == "one"
    assert second.text == "two"
    assert len(fake.calls) == 2
    assert fake.calls[0].messages == tuple(messages)
    assert fake.calls[0].max_tokens == 4096


async def test_exhausted_responses_raise() -> None:
    fake = FakeLLM([_response("only")])
    await fake.complete(messages=[LLMMessage(role="user", content="go")])

    with pytest.raises(RuntimeError, match="no scripted responses"):
        await fake.complete(messages=[LLMMessage(role="user", content="go")])


async def test_records_through_price_table_when_supplied() -> None:
    recorder = RecordingRecorder()
    fake = FakeLLM([_response("priced")], recorder=recorder, price_table=_table())

    response = await fake.complete(messages=[LLMMessage(role="user", content="go")])

    assert response.text == "priced"
    assert len(recorder.records) == 1
    usage, cost, tier = recorder.records[0]
    assert usage.output_tokens == 3
    assert cost == Decimal("0.000006")
    assert tier is None


async def test_records_zero_cost_when_no_price_table() -> None:
    recorder = RecordingRecorder()
    fake = FakeLLM([_response("zero")], recorder=recorder)

    await fake.complete(messages=[LLMMessage(role="user", content="go")])

    assert recorder.records[0][1] == Decimal(0)
    assert recorder.records[0][2] is None


async def test_no_recorder_is_fine() -> None:
    fake = FakeLLM([_response("silent")])
    response = await fake.complete(messages=[LLMMessage(role="user", content="go")])
    assert response.text == "silent"
