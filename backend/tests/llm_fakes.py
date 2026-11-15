"""Scripted tool-chat LLM fakes for the native runtime tests (doc 10b)."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass

from gaia.llm.base import LLMMessage, LLMResponse, LLMTool, LLMUsage


@dataclass(frozen=True)
class ToolChatCall:
    """One call made to :class:`ScriptedToolChatLLM`."""

    messages: tuple[LLMMessage, ...]
    tools: tuple[LLMTool, ...]
    max_tokens: int


class ScriptedToolChatLLM:
    """Return scripted responses, recording every request and tool list.

    ``block`` lets a test keep ``complete`` pending until the runtime is
    cancelled or timed out; ``delay_s`` is a shorter convenience for timeout
    tests.
    """

    def __init__(
        self,
        responses: Sequence[LLMResponse],
        *,
        block: asyncio.Event | None = None,
        delay_s: float = 0.0,
    ) -> None:
        self._responses = list(responses)
        self._block = block
        self._delay_s = delay_s
        self.calls: list[ToolChatCall] = []
        self.started = asyncio.Event()
        self.cancelled = False

    async def complete(
        self,
        *,
        messages: Sequence[LLMMessage],
        tools: Sequence[LLMTool] = (),
        max_tokens: int = 4096,
    ) -> LLMResponse:
        """Record the request, optionally block, then pop the next response."""
        self.calls.append(ToolChatCall(tuple(messages), tuple(tools), max_tokens))
        self.started.set()
        if self._delay_s:
            try:
                await asyncio.sleep(self._delay_s)
            except asyncio.CancelledError:
                self.cancelled = True
                raise
        if self._block is not None:
            try:
                await self._block.wait()
            except asyncio.CancelledError:
                self.cancelled = True
                raise
        if not self._responses:
            raise RuntimeError("ScriptedToolChatLLM has no scripted responses left")
        return self._responses.pop(0)


def make_usage(
    *,
    input_tokens: int = 10,
    output_tokens: int = 5,
    cache_read_tokens: int = 2,
    cache_creation_tokens: int = 0,
    model: str = "deepseek-v4-pro",
) -> LLMUsage:
    """Build a small DeepSeek usage value for scripted responses."""
    return LLMUsage(
        provider="deepseek",
        model=model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_tokens=cache_read_tokens,
        cache_creation_tokens=cache_creation_tokens,
        request_id="req_test",
    )
