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
from typing import Any, TypeVar

import openai
import structlog
from pydantic import BaseModel, SecretStr, ValidationError

from gaia.llm.base import (
    LLMBudgetExceeded,
    LLMMessage,
    LLMResponse,
    LLMStop,
    LLMTool,
    LLMUsage,
    ParsedResponse,
    ToolCall,
    UsageRecorder,
)
from gaia.llm.metering import deepseek_token_buckets
from gaia.services.costs import PriceTable

logger = structlog.get_logger(__name__)

#: OpenAI-compatible finish reasons that mean "did not produce a normal reply".
_FATAL_FINISH_REASONS = frozenset({"length", "content_filter"})

#: Parse calls are non-streaming on purpose: DeepSeek has no schema mode and
#: the OpenAI SDK's non-streaming JSON-mode path keeps thinking at the provider
#: default (doc 10b, P6).
PARSE_MAX_TOKENS = 16000

_ParseModelT = TypeVar("_ParseModelT", bound=BaseModel)


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


def _completion_choice(completion: Any) -> Any:
    """Return the first choice of an OpenAI completion, or raise."""
    choices = getattr(completion, "choices", None) or []
    if not choices:
        raise ValueError("DeepSeek returned no choices")
    return choices[0]


def _usage_from_completion(completion: Any, model: str) -> LLMUsage:
    """Read one OpenAI completion into Gaia's neutral usage shape."""
    usage_obj = getattr(completion, "usage", None)
    prompt_tokens = _field(usage_obj, "prompt_tokens")
    completion_tokens = _field(usage_obj, "completion_tokens")
    cache_hit_tokens = _field(usage_obj, "prompt_cache_hit_tokens")
    cache_miss_tokens = _field(usage_obj, "prompt_cache_miss_tokens")
    input_tokens, cache_read_tokens = deepseek_token_buckets(
        prompt_tokens=prompt_tokens,
        cache_hit_tokens=cache_hit_tokens,
        cache_miss_tokens=cache_miss_tokens,
    )
    return LLMUsage(
        provider="deepseek",
        model=getattr(completion, "model", None) or model,
        input_tokens=input_tokens,
        output_tokens=completion_tokens,
        cache_read_tokens=cache_read_tokens,
        cache_creation_tokens=0,
        request_id=_request_id(completion),
    )


def _parse_message(completion: Any) -> Any:
    """Return the message object of one OpenAI completion."""
    return getattr(_completion_choice(completion), "message", None)


def _message_content(message: Any) -> str:
    """Read ``message.content`` defensively, defaulting to an empty string."""
    return getattr(message, "content", None) or ""


def _budget_exceeded_body(exc: openai.APIStatusError) -> bool:
    """Return True when an OpenAI SDK error body is a gateway budget refusal."""
    body = getattr(exc, "body", None)
    if isinstance(body, dict):
        error = body.get("error")
        if isinstance(error, dict) and error.get("code") == "budget_exceeded":
            return True
    return getattr(exc, "code", None) == "budget_exceeded"


def _format_parse_validation_errors(exc: ValidationError) -> str:
    """Format one Pydantic validation error compactly for a repair prompt."""
    lines = [
        f"{'.'.join(str(part) for part in error.get('loc', ()))}: {error.get('msg', 'invalid')}"
        for error in exc.errors()
    ]
    return "\n".join(lines)[:2000]


def _json_schema_system(system: str, output_model: type[BaseModel]) -> str:
    """Append DeepSeek's JSON-mode instruction after the caller's system text."""
    schema = json.dumps(output_model.model_json_schema(), sort_keys=True)
    instruction = (
        f"Respond with a single JSON object that validates against this JSON schema:\n{schema}"
    )
    return f"{system}\n\n{instruction}" if system else instruction


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
        # An optional transport for the real OpenAI client. Production leaves
        # this ``None``; the in-process gateway test supplies an ASGI transport.
        http_client: Any | None = None,
    ) -> None:
        price_table.require(model)
        self._model = model
        self._base_url = base_url
        self._recorder = recorder
        self._price_table = price_table
        self._client = client or openai.AsyncOpenAI(
            api_key=api_key.get_secret_value(),
            base_url=base_url,
            http_client=http_client,
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
        except openai.PermissionDeniedError as exc:
            if _budget_exceeded_body(exc):
                raise LLMBudgetExceeded() from exc
            logger.error(
                "deepseek.api_status_error",
                model=self._model,
                request_id=exc.request_id,
                exc_info=exc,
            )
            raise
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

    async def aclose(self) -> None:
        """Close the underlying OpenAI client, if it has an async close method."""
        close = getattr(self._client, "close", None)
        if close is not None:
            await close()

    async def parse(
        self,
        *,
        system: str,
        messages: Sequence[LLMMessage],
        output_model: type[_ParseModelT],
        max_tokens: int = PARSE_MAX_TOKENS,
        enable_fallback: bool = False,
    ) -> ParsedResponse[_ParseModelT]:
        """Return one parsed JSON-mode completion, repairing invalid JSON once.

        DeepSeek's JSON mode has no schema support; the schema travels in the
        system prompt instead. Every call is recorded before it is parsed, and
        blank or invalid content gets exactly one repair call before
        :class:`LLMStop("invalid_output")`.
        """
        del enable_fallback  # DeepSeek has no server-side refusal fallback
        openai_messages: list[dict[str, Any]] = [
            {"role": "system", "content": _json_schema_system(system, output_model)},
            *_to_openai_messages(messages),
        ]
        request: dict[str, Any] = {
            "model": self._model,
            "messages": openai_messages,
            "max_tokens": max_tokens,
            "response_format": {"type": "json_object"},
        }

        completion = await self._create_parse_completion(request)
        parsed, error = await self._parse_once(completion, output_model)
        if parsed is not None:
            return parsed

        repair_messages = [
            *openai_messages,
            {"role": "assistant", "content": _message_content(_parse_message(completion))},
            {
                "role": "user",
                "content": (
                    "Your response was not valid JSON. Fix it and return only the corrected "
                    f"JSON object.\nValidation error:\n{error[:2000]}"
                ),
            },
        ]
        completion = await self._create_parse_completion({**request, "messages": repair_messages})
        parsed, _ = await self._parse_once(completion, output_model)
        if parsed is None:
            raise LLMStop("invalid_output", request_id=_request_id(completion))
        return parsed

    async def _create_parse_completion(self, request: dict[str, Any]) -> Any:
        """Send one JSON-mode completion and translate budget refusals."""
        try:
            return await self._client.chat.completions.create(**request)
        except openai.PermissionDeniedError as exc:
            if _budget_exceeded_body(exc):
                raise LLMBudgetExceeded() from exc
            logger.error(
                "deepseek.parse_api_status_error",
                model=self._model,
                request_id=exc.request_id,
                exc_info=exc,
            )
            raise
        except openai.APIStatusError as exc:
            logger.error(
                "deepseek.parse_api_status_error",
                model=self._model,
                request_id=exc.request_id,
                exc_info=exc,
            )
            raise
        except openai.APIConnectionError as exc:
            logger.error("deepseek.parse_connection_error", model=self._model, exc_info=exc)
            raise

    async def _parse_once(
        self,
        completion: Any,
        output_model: type[_ParseModelT],
    ) -> tuple[ParsedResponse[_ParseModelT] | None, str]:
        """Record one JSON-mode completion and validate its parsed output."""
        choice = _completion_choice(completion)
        finish_reason = getattr(choice, "finish_reason", None) or "stop"
        usage = _usage_from_completion(completion, self._model)
        request_id = usage.request_id
        quote = self._price_table.quote_or_price_as(usage, at=_utcnow(), fallback_model=self._model)
        await self._recorder.record(usage, cost_usd=quote.cost_usd, price_tier=quote.price_tier)

        logger.info(
            "deepseek.parse_recorded",
            model=usage.model,
            request_id=request_id,
            finish_reason=finish_reason,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cache_read_tokens=usage.cache_read_tokens,
            cost_usd=str(quote.cost_usd),
            price_tier=quote.price_tier,
        )

        if finish_reason == "length":
            raise LLMStop("max_tokens", request_id=request_id)
        if finish_reason == "content_filter":
            raise LLMStop("refusal", refusal_category="content_filter", request_id=request_id)

        content = _message_content(_parse_message(completion)).strip()
        if not content:
            return None, "the model returned only spaces"
        try:
            output = output_model.model_validate_json(content)
        except ValidationError as exc:
            return None, _format_parse_validation_errors(exc)
        return (
            ParsedResponse(
                output=output,
                usage=usage,
                model=usage.model,
                request_id=request_id,
                stop_reason=finish_reason,
            ),
            "",
        )

    async def _handle_response(self, completion: Any) -> LLMResponse:
        """Parse, log, price and record one OpenAI completion object."""
        choice = _completion_choice(completion)
        message = getattr(choice, "message", None)
        finish_reason = getattr(choice, "finish_reason", None) or "stop"

        usage = _usage_from_completion(completion, self._model)
        request_id = usage.request_id
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
