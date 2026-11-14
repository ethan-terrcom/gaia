"""Shared LLM value objects and the usage-recorder seam (doc 07).

The provider clients return the same small dataclasses instead of leaking
SDK-specific response types into the rest of Gaia. ``LLMUsage`` is the
provider-neutral token accounting shape: ``input_tokens`` means "tokens billed
at the base input rate" (for DeepSeek that is ``prompt_cache_miss_tokens``,
because cache misses are the only prompt tokens billed at that rate) and
``cache_read_tokens`` means "tokens billed at the cache-read/hit rate".
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Literal, Protocol, runtime_checkable


@dataclass(frozen=True)
class ToolCall:
    """One tool invocation requested by a model."""

    id: str
    name: str
    arguments: dict[str, Any]


@dataclass(frozen=True)
class LLMMessage:
    """A provider-neutral chat message.

    ``reasoning_content`` and ``tool_calls`` exist for DeepSeek thinking-mode
    tool loops: the assistant message must be passed back with those fields
    intact. Anthropic tool loops are out of scope for 07a, so the Anthropic
    client only converts plain user/assistant text messages.
    """

    role: Literal["system", "user", "assistant", "tool"]
    content: str
    name: str | None = None
    tool_call_id: str | None = None
    tool_calls: tuple[ToolCall, ...] = ()
    reasoning_content: str | None = None


@dataclass(frozen=True)
class LLMTool:
    """A tool definition in the JSON-schema shape both providers accept."""

    name: str
    description: str
    input_schema: dict[str, Any]


@dataclass(frozen=True)
class LLMUsage:
    """Token counts for one provider call.

    All four token fields are non-negative integers and use the common billing
    buckets documented in ``gaia/llm/prices.toml``. ``request_id`` is the
    provider's request id, used to reconcile Gaia's rows with a provider bill.
    """

    provider: str
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0
    request_id: str | None = None


@dataclass(frozen=True)
class LLMResponse:
    """A successful provider turn, already priced and recorded by the client."""

    text: str
    usage: LLMUsage
    tool_calls: tuple[ToolCall, ...] = ()
    stop_reason: str = "end_turn"
    model: str = ""
    request_id: str | None = None
    #: DeepSeek thinking-mode output. A tool loop must send it back on the
    #: assistant message (``LLMMessage.reasoning_content``) with the tool calls.
    reasoning_content: str | None = None


class LLMStop(Exception):
    """A provider stopped in a way the caller must not treat as a normal reply.

    Raised for refusal, ``max_tokens``/``length`` and an unexpected
    ``pause_turn``. The call's usage has already been recorded before this is
    raised, because providers bill for refused and truncated turns too.
    """

    def __init__(
        self,
        stop_reason: str,
        *,
        refusal_category: str | None = None,
        request_id: str | None = None,
    ) -> None:
        self.stop_reason = stop_reason
        self.refusal_category = refusal_category
        self.request_id = request_id
        message = f"LLM call stopped: {stop_reason}"
        if refusal_category is not None:
            message = f"{message} ({refusal_category})"
        super().__init__(message)


@runtime_checkable
class UsageRecorder(Protocol):
    """Persist one priced :class:`LLMUsage` (doc 07).

    Implementations are bound to call context (run, step, agent, workspace)
    where they are constructed; the provider clients only add the token counts
    and the price quote.
    """

    async def record(
        self,
        usage: LLMUsage,
        *,
        cost_usd: Decimal,
        price_tier: str | None = None,
    ) -> None:
        """Write one ``llm_usage`` row."""
        ...
