"""OpenAI-compatible chat-completions client for DeepSeek, OpenAI and xAI (doc 07, 19a).

This is the one module that imports the ``openai`` SDK (ADR-001 P1). The three
providers speak the same wire format with small dialect differences, which are
the only provider-specific code here:

- **Output cap.** DeepSeek takes ``max_tokens``; OpenAI's reasoning models
  reject it and take ``max_completion_tokens``, which xAI accepts too.
- **Reasoning passback.** DeepSeek's thinking mode returns
  ``message.reasoning_content`` and a tool loop must send it back on the
  assistant message. Other providers never get the field.
- **Usage.** Cached prompt tokens and reasoning tokens are reported three
  different ways; :func:`gaia.llm.metering.openai_compat_buckets` reads them all.

Provider-specific fields are not in the OpenAI SDK types, so they are read
defensively with ``getattr`` and ``model_extra``.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any, TypeVar

import openai
import structlog
from pydantic import BaseModel, SecretStr, ValidationError

from gaia.domain.enums import LLMProvider
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
from gaia.llm.metering import openai_compat_buckets
from gaia.services.costs import PriceTable

logger = structlog.get_logger(__name__)

#: OpenAI-compatible finish reasons that mean "did not produce a normal reply".
_FATAL_FINISH_REASONS = frozenset({"length", "content_filter"})

#: Parse calls are non-streaming on purpose: JSON mode (``json_object``) is the
#: one structured-output form all three providers accept, and the OpenAI SDK's
#: non-streaming JSON-mode path keeps thinking at the provider default (doc 10b,
#: P6).
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


def _to_openai_messages(
    messages: Sequence[LLMMessage], *, reasoning_passback: bool = True
) -> list[dict[str, Any]]:
    """Convert Gaia messages to OpenAI chat message dicts.

    ``reasoning_passback`` is DeepSeek's dialect: its thinking-mode tool loops
    need ``reasoning_content`` echoed on the assistant message. Other providers
    would reject or ignore an unknown message field, so it is left off.
    """
    converted: list[dict[str, Any]] = []
    for message in messages:
        if message.role == "system":
            converted.append({"role": "system", "content": message.content})
        elif message.role == "user":
            converted.append({"role": "user", "content": message.content})
        elif message.role == "assistant":
            assistant: dict[str, Any] = {"role": "assistant", "content": message.content or None}
            if reasoning_passback and message.reasoning_content is not None:
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
            raise ValueError(f"unsupported chat message role: {message.role}")
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
        raise ValueError("the provider returned no choices")
    return choices[0]


#: Integer usage fields read from the top level of an OpenAI-compatible usage object.
_USAGE_FIELDS = (
    "prompt_tokens",
    "completion_tokens",
    "total_tokens",
    "prompt_cache_hit_tokens",
    "prompt_cache_miss_tokens",
)


def _details_field(usage_obj: Any, details: str, name: str) -> int:
    """Read ``usage.<details>.<name>``, which may be an object, a dict or absent."""
    container = getattr(usage_obj, details, None)
    if container is None:
        extra = getattr(usage_obj, "model_extra", None) or {}
        container = extra.get(details)
    if isinstance(container, dict):
        value = container.get(name)
        return 0 if value is None else int(value)
    return _field(container, name)


def _usage_dict(usage_obj: Any) -> dict[str, Any]:
    """Flatten an SDK usage object into the dict shape the gateway meters."""
    usage: dict[str, Any] = {name: _field(usage_obj, name) for name in _USAGE_FIELDS}
    usage["prompt_tokens_details"] = {
        "cached_tokens": _details_field(usage_obj, "prompt_tokens_details", "cached_tokens")
    }
    return usage


def _usage_from_completion(completion: Any, model: str, provider: str) -> LLMUsage:
    """Read one OpenAI completion into Gaia's neutral usage shape."""
    usage_obj = getattr(completion, "usage", None)
    input_tokens, cache_read_tokens, output_tokens = openai_compat_buckets(_usage_dict(usage_obj))
    return LLMUsage(
        provider=provider,
        model=getattr(completion, "model", None) or model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
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
    """Append the JSON-mode instruction after the caller's system text.

    OpenAI's JSON mode also requires the word "JSON" somewhere in the messages;
    this instruction always contains it.
    """
    schema = json.dumps(output_model.model_json_schema(), sort_keys=True)
    instruction = (
        f"Respond with a single JSON object that validates against this JSON schema:\n{schema}"
    )
    return f"{system}\n\n{instruction}" if system else instruction


class OpenAICompatClient:
    """Async client for one model on an OpenAI-compatible chat-completions API."""

    def __init__(
        self,
        *,
        provider: str,
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
        self._provider = LLMProvider(provider)
        if self._provider is LLMProvider.ANTHROPIC:
            raise ValueError(
                "Anthropic models use AnthropicClient, not the OpenAI-compatible client"
            )
        price_table.require(model)
        self._model = model
        self._base_url = base_url
        self._log = logger.bind(provider=self._provider.value)
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
            "messages": self._messages(messages),
            self._token_limit_param: max_tokens,
        }
        if tools:
            request["tools"] = _to_openai_tools(tools)

        try:
            completion = await self._client.chat.completions.create(**request)
        except openai.PermissionDeniedError as exc:
            if _budget_exceeded_body(exc):
                raise LLMBudgetExceeded() from exc
            self._log.error(
                "llm_compat.api_status_error",
                model=self._model,
                request_id=exc.request_id,
                exc_info=exc,
            )
            raise
        except openai.RateLimitError as exc:
            self._log.error(
                "llm_compat.rate_limited",
                model=self._model,
                request_id=exc.request_id,
                exc_info=exc,
            )
            raise
        except openai.APIStatusError as exc:
            self._log.error(
                "llm_compat.api_status_error",
                model=self._model,
                request_id=exc.request_id,
                exc_info=exc,
            )
            raise
        except openai.APIConnectionError as exc:
            self._log.error("llm_compat.connection_error", model=self._model, exc_info=exc)
            raise

        return await self._handle_response(completion)

    @property
    def provider(self) -> str:
        """The provider this client calls, as a :class:`LLMProvider` value."""
        return self._provider.value

    @property
    def _token_limit_param(self) -> str:
        """The request field that caps output tokens for this provider's dialect."""
        return "max_tokens" if self._provider is LLMProvider.DEEPSEEK else "max_completion_tokens"

    def _messages(self, messages: Sequence[LLMMessage]) -> list[dict[str, Any]]:
        """Convert messages for this provider's dialect."""
        return _to_openai_messages(
            messages, reasoning_passback=self._provider is LLMProvider.DEEPSEEK
        )

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

        ``json_object`` mode has no schema support on DeepSeek, so the schema
        travels in the system prompt for every provider (one code path, the
        same Pydantic validation and repair). Every call is recorded before it is parsed, and
        blank or invalid content gets exactly one repair call before
        :class:`LLMStop("invalid_output")`.
        """
        del enable_fallback  # these providers have no server-side refusal fallback
        openai_messages: list[dict[str, Any]] = [
            {"role": "system", "content": _json_schema_system(system, output_model)},
            *self._messages(messages),
        ]
        request: dict[str, Any] = {
            "model": self._model,
            "messages": openai_messages,
            self._token_limit_param: max_tokens,
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
            self._log.error(
                "llm_compat.parse_api_status_error",
                model=self._model,
                request_id=exc.request_id,
                exc_info=exc,
            )
            raise
        except openai.APIStatusError as exc:
            self._log.error(
                "llm_compat.parse_api_status_error",
                model=self._model,
                request_id=exc.request_id,
                exc_info=exc,
            )
            raise
        except openai.APIConnectionError as exc:
            self._log.error("llm_compat.parse_connection_error", model=self._model, exc_info=exc)
            raise

    async def _parse_once(
        self,
        completion: Any,
        output_model: type[_ParseModelT],
    ) -> tuple[ParsedResponse[_ParseModelT] | None, str]:
        """Record one JSON-mode completion and validate its parsed output."""
        choice = _completion_choice(completion)
        finish_reason = getattr(choice, "finish_reason", None) or "stop"
        usage = _usage_from_completion(completion, self._model, self._provider.value)
        request_id = usage.request_id
        quote = self._price_table.quote_or_price_as(usage, at=_utcnow(), fallback_model=self._model)
        await self._recorder.record(usage, cost_usd=quote.cost_usd, price_tier=quote.price_tier)

        self._log.info(
            "llm_compat.parse_recorded",
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

        usage = _usage_from_completion(completion, self._model, self._provider.value)
        request_id = usage.request_id
        quote = self._price_table.quote_or_price_as(usage, at=_utcnow(), fallback_model=self._model)
        await self._recorder.record(usage, cost_usd=quote.cost_usd, price_tier=quote.price_tier)

        self._log.info(
            "llm_compat.call_recorded",
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


class DeepSeekClient(OpenAICompatClient):
    """The OpenAI-compatible client with DeepSeek's dialect (doc 07)."""

    def __init__(
        self,
        *,
        api_key: SecretStr,
        model: str,
        base_url: str,
        recorder: UsageRecorder,
        price_table: PriceTable,
        client: Any | None = None,
        http_client: Any | None = None,
    ) -> None:
        super().__init__(
            provider=LLMProvider.DEEPSEEK.value,
            api_key=api_key,
            model=model,
            base_url=base_url,
            recorder=recorder,
            price_table=price_table,
            client=client,
            http_client=http_client,
        )
