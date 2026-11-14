"""DeepSeek client over the OpenAI-compatible ``chat.completions`` API (doc 07).

DeepSeek's thinking-mode output adds ``message.reasoning_content`` and its usage
adds ``prompt_cache_hit_tokens``/``prompt_cache_miss_tokens``. Those fields are
not in the OpenAI SDK types, so they are read defensively with ``getattr`` and
``model_extra``.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

import openai
import structlog
from pydantic import SecretStr

from gaia.llm.base import (
    LLMMessage,
    LLMResponse,
    LLMStop,
    LLMTool,
    LLMUsage,
    ToolCall,
    UsageRecorder,
)
from gaia.services.costs import PriceTable

logger = structlog.get_logger(__name__)

#: OpenAI-compatible finish reasons that mean "did not produce a normal reply".
_FATAL_FINISH_REASONS = frozenset({"length", "content_filter"})


def _utcnow() -> datetime:
    """Return the current aware UTC time (a function so tests can patch it)."""
    return datetime.now(UTC)


def _request_id(completion: Any) -> str | None:
    """Read the OpenAI SDK's public ``_request_id`` property, if present."""
    return getattr(completion, "_request_id", None)


def _field(obj: Any, name: str, *, default: int = 0) -> int:
    """Read an integer field that may live in ``model_extra`` instead."""
    if obj is None:
        return default
    value = getattr(obj, name, None)
    if value is None:
        extra = getattr(obj, "model_extra", None)
        if extra:
            value = extra.get(name)
    if value is None:
        return default
    return int(value)


def _reasoning_content(message: Any) -> str | None:
    """Read DeepSeek's ``reasoning_content``, which may live in ``model_extra``."""
    value = getattr(message, "reasoning_content", None)
    if value is None:
        extra = getattr(message, "model_extra", None) or {}
        value = extra.get("reasoning_content")
    return None if value is None else str(value)


def _to_openai_messages(messages: Sequence[LLMMessage]) -> list[dict[str, Any]]:
    """Convert Gaia messages to OpenAI chat message dicts."""
    converted: list[dict[str, Any]] = []
    for message in messages:
        if message.role == "system":
            converted.append({"role": "system", "content": message.content})
        elif message.role == "user":
            converted.append({"role": "user", "content": message.content})
        elif message.role == "assistant":
            assistant: dict[str, Any] = {"role": "assistant", "content": message.content or None}
            if message.reasoning_content is not None:
                assistant["reasoning_content"] = message.reasoning_content
            if message.tool_calls:
                assistant["tool_calls"] = [
                    {
                        "id": tool_call.id,
                        "type": "function",
                        "function": {
                            "name": tool_call.name,
                            "arguments": json.dumps(tool_call.arguments),
                        },
                    }
                    for tool_call in message.tool_calls
                ]
            converted.append(assistant)
        elif message.role == "tool":
            converted.append(
                {
                    "role": "tool",
                    "tool_call_id": message.tool_call_id,
                    "content": message.content,
                }
            )
        else:
            raise ValueError(f"unsupported DeepSeek message role: {message.role}")
    return converted


def _to_openai_tools(tools: Sequence[LLMTool]) -> list[dict[str, Any]]:
    """Convert Gaia tools to OpenAI function-calling tool dicts."""
    return [
        {
            "type": "function",
            "function": {
                "name": tool.name,
                "description": tool.description,
                "parameters": tool.input_schema,
            },
        }
        for tool in tools
    ]


def _parse_tool_calls(message: Any) -> tuple[ToolCall, ...]:
    """Read OpenAI function tool calls into Gaia's neutral shape."""
    tool_calls: list[ToolCall] = []
    for tool_call in getattr(message, "tool_calls", None) or []:
        function = getattr(tool_call, "function", None)
        if function is None:
            continue
        arguments_text = getattr(function, "arguments", None) or "{}"
        try:
            arguments = json.loads(arguments_text)
        except json.JSONDecodeError:
            arguments = {}
        if not isinstance(arguments, dict):
            arguments = {}
        tool_calls.append(
            ToolCall(
                id=str(getattr(tool_call, "id", "") or ""),
                name=str(getattr(function, "name", "") or ""),
                arguments=arguments,
            )
        )
    return tuple(tool_calls)


class DeepSeekClient:
    """Async client for one DeepSeek model over OpenAI's chat completions."""

    def __init__(
        self,
        *,
        api_key: SecretStr,
        model: str,
        base_url: str,
        recorder: UsageRecorder,
        price_table: PriceTable,
        # See the comment in AnthropicClient: the OpenAI SDK signatures are
        # generated and version-specific, so this seam accepts a stub in tests.
        client: Any | None = None,
    ) -> None:
        price_table.require(model)
        self._model = model
        self._base_url = base_url
        self._recorder = recorder
        self._price_table = price_table
        self._client = client or openai.AsyncOpenAI(
            api_key=api_key.get_secret_value(),
            base_url=base_url,
        )

    async def complete(
        self,
        *,
        messages: Sequence[LLMMessage],
        tools: Sequence[LLMTool] = (),
        max_tokens: int = 4096,
    ) -> LLMResponse:
        """Send one chat completion, record it, and return a neutral response."""
        request: dict[str, Any] = {
            "model": self._model,
            "messages": _to_openai_messages(messages),
            "max_tokens": max_tokens,
        }
        if tools:
            request["tools"] = _to_openai_tools(tools)

        try:
            completion = await self._client.chat.completions.create(**request)
        except openai.RateLimitError as exc:
            logger.error(
                "deepseek.rate_limited", model=self._model, request_id=exc.request_id, exc_info=exc
            )
            raise
        except openai.APIStatusError as exc:
            logger.error(
                "deepseek.api_status_error",
                model=self._model,
                request_id=exc.request_id,
                exc_info=exc,
            )
            raise
        except openai.APIConnectionError as exc:
            logger.error("deepseek.connection_error", model=self._model, exc_info=exc)
            raise

        return await self._handle_response(completion)

    async def _handle_response(self, completion: Any) -> LLMResponse:
        """Parse, log, price and record one OpenAI completion object."""
        choices = getattr(completion, "choices", None) or []
        if not choices:
            raise ValueError("DeepSeek returned no choices")
        choice = choices[0]
        message = getattr(choice, "message", None)
        finish_reason = getattr(choice, "finish_reason", None) or "stop"

        usage_obj = getattr(completion, "usage", None)
        prompt_tokens = _field(usage_obj, "prompt_tokens")
        completion_tokens = _field(usage_obj, "completion_tokens")
        cache_hit_tokens = _field(usage_obj, "prompt_cache_hit_tokens")
        cache_miss_tokens = _field(usage_obj, "prompt_cache_miss_tokens")
        if cache_miss_tokens == 0 and cache_hit_tokens:
            cache_miss_tokens = max(0, prompt_tokens - cache_hit_tokens)
        elif cache_hit_tokens == 0 and cache_miss_tokens == 0:
            cache_miss_tokens = prompt_tokens

        request_id = _request_id(completion)
        usage = LLMUsage(
            provider="deepseek",
            model=getattr(completion, "model", None) or self._model,
            input_tokens=cache_miss_tokens,
            output_tokens=completion_tokens,
            cache_read_tokens=cache_hit_tokens,
            cache_creation_tokens=0,
            request_id=request_id,
        )
        quote = self._price_table.quote_or_price_as(usage, at=_utcnow(), fallback_model=self._model)
        await self._recorder.record(usage, cost_usd=quote.cost_usd, price_tier=quote.price_tier)

        logger.info(
            "deepseek.call_recorded",
            model=usage.model,
            request_id=request_id,
            finish_reason=finish_reason,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cache_read_tokens=usage.cache_read_tokens,
            cost_usd=str(quote.cost_usd),
            price_tier=quote.price_tier,
        )

        if finish_reason in _FATAL_FINISH_REASONS:
            if finish_reason == "length":
                raise LLMStop("max_tokens", request_id=request_id)
            raise LLMStop("refusal", refusal_category="content_filter", request_id=request_id)

        text = getattr(message, "content", None) or ""
        tool_calls = _parse_tool_calls(message)
        return LLMResponse(
            text=text,
            usage=usage,
            tool_calls=tool_calls,
            stop_reason=finish_reason,
            model=usage.model,
            request_id=request_id,
            reasoning_content=_reasoning_content(message),
        )
