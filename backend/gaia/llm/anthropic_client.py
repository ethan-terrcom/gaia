"""Anthropic Messages API client for the lead model (doc 07 task 2).

The client is intentionally thin. It converts Gaia's provider-neutral messages
and tools to the Anthropic wire shape, streams large requests, turns on prompt
caching for the stable system prompt, enables the server-side refusal fallback
when asked, records usage at the model that actually served the request, and
raises :class:`LLMStop` for stops that must not be returned as text.

TODO(gaia-portability): Anthropic tool-loop adapter (ADR-001 P6).
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any, TypeVar

import anthropic
import structlog
from pydantic import BaseModel, SecretStr

from gaia.llm.base import (
    LLMMessage,
    LLMResponse,
    LLMStop,
    LLMTool,
    LLMUsage,
    ParsedResponse,
    ToolCall,
    UsageRecorder,
)
from gaia.services.costs import PriceTable

logger = structlog.get_logger(__name__)

#: Opus 5.5 rejects forced tool_choice; ``auto`` is the only safe value (doc 07).
TOOL_CHOICE_AUTO: dict[str, str] = {"type": "auto"}
#: Opus 5.5 defaults to ``medium``; doc 07 wants ``high`` set explicitly.
OUTPUT_CONFIG_HIGH: dict[str, str] = {"effort": "high"}
#: Top-level prompt-cache marker; the SDK applies it to the last cacheable block.
CACHE_CONTROL_EPHEMERAL: dict[str, str] = {"type": "ephemeral"}
#: Above this many output tokens the SDK's stream helper is required.
STREAM_MAX_TOKENS_THRESHOLD = 8192
#: Parse calls cannot stream in anthropic 2.8.0, so keep them non-streaming.
PARSE_MAX_TOKENS = 16000
#: Beta header enabling server-side refusal fallback.
SERVER_SIDE_FALLBACK_BETA = "server-side-fallback-2026-07-01"

_ParseModelT = TypeVar("_ParseModelT", bound=BaseModel)

#: Stop reasons that must surface as :class:`LLMStop`, not as an empty reply.
_FATAL_STOP_REASONS = frozenset(
    {"max_tokens", "pause_turn", "refusal", "model_context_window_exceeded"}
)


def _utcnow() -> datetime:
    """Return the current aware UTC time (a function so tests can patch it)."""
    return datetime.now(UTC)


def _as_int(value: Any, *, default: int = 0) -> int:
    """Read an SDK usage field that may be ``None`` in older responses."""
    if value is None:
        return default
    return int(value)


def _request_id(message: Any) -> str | None:
    """Read the SDK's public ``_request_id`` property, if it is present."""
    return getattr(message, "_request_id", None)


def _to_anthropic_messages(messages: Sequence[LLMMessage]) -> list[dict[str, Any]]:
    """Convert Gaia messages to Anthropic ``messages`` entries."""
    converted: list[dict[str, Any]] = []
    for message in messages:
        if message.role == "user":
            converted.append({"role": "user", "content": message.content})
        elif message.role == "assistant":
            converted.append({"role": "assistant", "content": message.content})
        else:
            raise ValueError(f"Anthropic messages must be user/assistant, not {message.role}")
    return converted


def _to_anthropic_tools(tools: Sequence[LLMTool]) -> list[dict[str, Any]]:
    """Convert Gaia tools, with ``strict`` set for schema-guaranteed inputs."""
    return [
        {
            "name": tool.name,
            "description": tool.description,
            "input_schema": tool.input_schema,
            "strict": True,
        }
        for tool in tools
    ]


class AnthropicClient:
    """Async client for one Anthropic model, with usage recording."""

    def __init__(
        self,
        *,
        api_key: SecretStr,
        model: str,
        recorder: UsageRecorder,
        price_table: PriceTable,
        # SDK clients are generated with narrow version-specific signatures;
        # accepting the concrete client here would make tests unable to use a
        # stub and would make this seam depend on one SDK minor version.
        client: Any | None = None,
    ) -> None:
        price_table.require(model)
        self._model = model
        self._recorder = recorder
        self._price_table = price_table
        self._client = client or anthropic.AsyncAnthropic(api_key=api_key.get_secret_value())

    async def complete(
        self,
        *,
        system: str,
        messages: Sequence[LLMMessage],
        tools: Sequence[LLMTool] = (),
        max_tokens: int = 4096,
        enable_fallback: bool = False,
    ) -> LLMResponse:
        """Send one Messages API call, record it, and return a neutral response.

        Raises :class:`LLMStop` for refusal/max_tokens/pause_turn stops, after
        recording the usage. Provider errors are logged and re-raised; the SDK
        has already retried them twice.
        """
        try:
            message = await self._create_message(
                system=system,
                messages=messages,
                tools=tools,
                max_tokens=max_tokens,
                enable_fallback=enable_fallback,
            )
        except anthropic.RateLimitError as exc:
            logger.error(
                "anthropic.rate_limited", model=self._model, request_id=exc.request_id, exc_info=exc
            )
            raise
        except anthropic.APIStatusError as exc:
            logger.error(
                "anthropic.api_status_error",
                model=self._model,
                request_id=exc.request_id,
                exc_info=exc,
            )
            raise
        except anthropic.APIConnectionError as exc:
            logger.error("anthropic.connection_error", model=self._model, exc_info=exc)
            raise

        return await self._handle_response(message)

    async def parse(
        self,
        *,
        system: str,
        messages: Sequence[LLMMessage],
        output_model: type[_ParseModelT],
        max_tokens: int = PARSE_MAX_TOKENS,
        enable_fallback: bool = False,
    ) -> ParsedResponse[_ParseModelT]:
        """Send one structured-output Messages API call and return its output.

        Always **streamed** (``messages.stream(..., output_format=...)`` plus
        ``get_final_message()``), never through the SDK's non-streaming
        ``parse`` helper. A long Opus call with ``effort: high`` sends nothing
        while it thinks, and the doc 08 spike saw a non-streamed planning call
        die mid-flight. Streaming also accepts ``cache_control`` on the
        non-beta path, where ``parse`` does not. Usage is recorded before any
        stop is raised, exactly like :meth:`complete`.
        """
        try:
            message = await self._create_parse(
                system=system,
                messages=messages,
                output_model=output_model,
                max_tokens=max_tokens,
                enable_fallback=enable_fallback,
            )
        except anthropic.RateLimitError as exc:
            logger.error(
                "anthropic.parse_rate_limited",
                model=self._model,
                request_id=exc.request_id,
                exc_info=exc,
            )
            raise
        except anthropic.APIStatusError as exc:
            logger.error(
                "anthropic.parse_api_status_error",
                model=self._model,
                request_id=exc.request_id,
                exc_info=exc,
            )
            raise
        except anthropic.APIConnectionError as exc:
            logger.error("anthropic.parse_connection_error", model=self._model, exc_info=exc)
            raise

        return await self._handle_parse_response(message)

    async def _create_parse(
        self,
        *,
        system: str,
        messages: Sequence[LLMMessage],
        output_model: type[_ParseModelT],
        max_tokens: int,
        enable_fallback: bool,
    ) -> Any:
        """Call the SDK parse helper, using beta fallback when requested."""
        request: dict[str, Any] = {
            "model": self._model,
            "max_tokens": max_tokens,
            "messages": _to_anthropic_messages(messages),
            "system": system,
            "output_format": output_model,
            "output_config": OUTPUT_CONFIG_HIGH,
        }
        if system:
            request["cache_control"] = CACHE_CONTROL_EPHEMERAL
        messages_api = self._client.messages
        if enable_fallback:
            request["betas"] = [SERVER_SIDE_FALLBACK_BETA]
            request["fallbacks"] = "default"
            messages_api = self._client.beta.messages
        # Both ``messages.stream`` and ``beta.messages.stream`` accept
        # ``output_format`` in anthropic 2.8.0, and the final message carries
        # ``parsed_output`` like ``parse`` does (lead review 11, L1).
        async with messages_api.stream(**request) as stream:
            return await stream.get_final_message()

    async def _handle_parse_response(self, message: Any) -> ParsedResponse[_ParseModelT]:
        """Parse, log, price and record one structured SDK message object."""
        stop_reason = getattr(message, "stop_reason", None) or "end_turn"
        usage = LLMUsage(
            provider="anthropic",
            model=getattr(message, "model", None) or self._model,
            input_tokens=_as_int(getattr(getattr(message, "usage", None), "input_tokens", None)),
            output_tokens=_as_int(getattr(getattr(message, "usage", None), "output_tokens", None)),
            cache_read_tokens=_as_int(
                getattr(getattr(message, "usage", None), "cache_read_input_tokens", None)
            ),
            cache_creation_tokens=_as_int(
                getattr(getattr(message, "usage", None), "cache_creation_input_tokens", None)
            ),
            request_id=_request_id(message),
        )
        quote = self._price_table.quote_or_price_as(usage, at=_utcnow(), fallback_model=self._model)
        await self._recorder.record(usage, cost_usd=quote.cost_usd, price_tier=quote.price_tier)

        logger.info(
            "anthropic.parse_recorded",
            model=usage.model,
            request_id=usage.request_id,
            stop_reason=stop_reason,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cache_read_tokens=usage.cache_read_tokens,
            cache_creation_tokens=usage.cache_creation_tokens,
            cost_usd=str(quote.cost_usd),
            price_tier=quote.price_tier,
        )

        if stop_reason in _FATAL_STOP_REASONS:
            refusal_category: str | None = None
            if stop_reason == "refusal":
                stop_details = getattr(message, "stop_details", None)
                refusal_category = getattr(stop_details, "category", None)
            raise LLMStop(
                stop_reason,
                refusal_category=refusal_category,
                request_id=usage.request_id,
            )

        parsed_output = getattr(message, "parsed_output", None)
        if parsed_output is None:
            raise LLMStop("invalid_output", request_id=usage.request_id)

        return ParsedResponse(
            output=parsed_output,
            usage=usage,
            model=usage.model,
            request_id=usage.request_id,
            stop_reason=stop_reason,
        )

    async def _create_message(
        self,
        *,
        system: str,
        messages: Sequence[LLMMessage],
        tools: Sequence[LLMTool],
        max_tokens: int,
        enable_fallback: bool,
    ) -> Any:
        """Call the SDK, using the beta fallback resource when requested."""
        request: dict[str, Any] = {
            "model": self._model,
            "max_tokens": max_tokens,
            "messages": _to_anthropic_messages(messages),
            "system": system,
            "output_config": OUTPUT_CONFIG_HIGH,
        }
        if tools:
            # tool_choice is only valid alongside tools: the API answers 400
            # "tool_choice may only be specified while providing tools".
            request["tools"] = _to_anthropic_tools(tools)
            request["tool_choice"] = TOOL_CHOICE_AUTO
        if system:
            request["cache_control"] = CACHE_CONTROL_EPHEMERAL

        if enable_fallback:
            request["betas"] = [SERVER_SIDE_FALLBACK_BETA]
            request["fallbacks"] = "default"
            if max_tokens > STREAM_MAX_TOKENS_THRESHOLD:
                async with self._client.beta.messages.stream(**request) as stream:
                    return await stream.get_final_message()
            return await self._client.beta.messages.create(**request)

        if max_tokens > STREAM_MAX_TOKENS_THRESHOLD:
            async with self._client.messages.stream(**request) as stream:
                return await stream.get_final_message()
        return await self._client.messages.create(**request)

    async def _handle_response(self, message: Any) -> LLMResponse:
        """Parse, log, price and record one SDK message object."""
        stop_reason = getattr(message, "stop_reason", None) or "end_turn"
        usage = LLMUsage(
            provider="anthropic",
            model=getattr(message, "model", None) or self._model,
            input_tokens=_as_int(getattr(getattr(message, "usage", None), "input_tokens", None)),
            output_tokens=_as_int(getattr(getattr(message, "usage", None), "output_tokens", None)),
            cache_read_tokens=_as_int(
                getattr(getattr(message, "usage", None), "cache_read_input_tokens", None)
            ),
            cache_creation_tokens=_as_int(
                getattr(getattr(message, "usage", None), "cache_creation_input_tokens", None)
            ),
            request_id=_request_id(message),
        )
        quote = self._price_table.quote_or_price_as(usage, at=_utcnow(), fallback_model=self._model)
        await self._recorder.record(usage, cost_usd=quote.cost_usd, price_tier=quote.price_tier)

        logger.info(
            "anthropic.call_recorded",
            model=usage.model,
            request_id=usage.request_id,
            stop_reason=stop_reason,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cache_read_tokens=usage.cache_read_tokens,
            cache_creation_tokens=usage.cache_creation_tokens,
            cost_usd=str(quote.cost_usd),
            price_tier=quote.price_tier,
        )

        if stop_reason in _FATAL_STOP_REASONS:
            refusal_category: str | None = None
            if stop_reason == "refusal":
                stop_details = getattr(message, "stop_details", None)
                refusal_category = getattr(stop_details, "category", None)
            raise LLMStop(
                stop_reason,
                refusal_category=refusal_category,
                request_id=usage.request_id,
            )

        text = "".join(
            str(block.text)
            for block in getattr(message, "content", [])
            if getattr(block, "type", None) == "text"
        )
        tool_calls = tuple(
            ToolCall(
                id=str(block.id),
                name=str(block.name),
                arguments=dict(block.input or {}),
            )
            for block in getattr(message, "content", [])
            if getattr(block, "type", None) == "tool_use"
        )
        return LLMResponse(
            text=text,
            usage=usage,
            tool_calls=tool_calls,
            stop_reason=stop_reason,
            model=usage.model,
            request_id=usage.request_id,
        )
