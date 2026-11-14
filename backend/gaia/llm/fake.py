"""A scripted LLM provider for tests (doc 07 task 5)."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from gaia.llm.base import LLMMessage, LLMResponse, LLMTool, UsageRecorder
from gaia.services.costs import PriceTable


def _utcnow() -> datetime:
    """Return the current aware UTC time (a function so tests can patch it)."""
    return datetime.now(UTC)


@dataclass(frozen=True)
class FakeLLMCall:
    """One call made to :class:`FakeLLM`, for assertions in tests."""

    messages: tuple[LLMMessage, ...]
    tools: tuple[LLMTool, ...]
    max_tokens: int


class FakeLLM:
    """Return scripted responses in sequence, optionally recording their usage.

    When a recorder is supplied the scripted response's usage is priced with the
    supplied table (or recorded at zero cost when no table is given), which lets
    contract tests run the same recording path as a real provider without
    spending money or touching the network.
    """

    def __init__(
        self,
        responses: Sequence[LLMResponse],
        *,
        recorder: UsageRecorder | None = None,
        price_table: PriceTable | None = None,
    ) -> None:
        self._responses = list(responses)
        self._recorder = recorder
        self._price_table = price_table
        self.calls: list[FakeLLMCall] = []

    async def complete(
        self,
        *,
        messages: Sequence[LLMMessage],
        tools: Sequence[LLMTool] = (),
        max_tokens: int = 4096,
    ) -> LLMResponse:
        """Pop and return the next scripted response."""
        self.calls.append(FakeLLMCall(tuple(messages), tuple(tools), max_tokens))
        if not self._responses:
            raise RuntimeError("FakeLLM has no scripted responses left")
        response = self._responses.pop(0)
        if self._recorder is not None:
            if self._price_table is not None:
                quote = self._price_table.quote(response.usage, at=_utcnow())
                await self._recorder.record(
                    response.usage,
                    cost_usd=quote.cost_usd,
                    price_tier=quote.price_tier,
                )
            else:
                await self._recorder.record(response.usage, cost_usd=Decimal(0))
        return response
