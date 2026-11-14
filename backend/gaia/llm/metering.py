"""Streaming usage parsers for the LLM gateway (doc 07b).

The gateway relays raw bytes, so metering has to read token counts out of the
same bytes without calling a provider SDK. Two wire shapes are supported:

- **Anthropic Messages API**, both JSON and SSE. ``message_start`` carries the
  model and the base usage; ``message_delta`` carries ``usage.output_tokens``,
  which is cumulative, so the last value wins.
- **OpenAI-compatible chat completions** (DeepSeek), both JSON and SSE. The
  final chunk carries ``usage`` and ``model``; DeepSeek's cache-miss/cache-hit
  fields become Gaia's ``input_tokens``/``cache_read_tokens`` buckets via the
  same helper ``DeepSeekClient`` uses.

This module is pure and does no I/O.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any, Literal

from gaia.llm.base import LLMUsage


def _as_int(value: Any, *, default: int = 0) -> int:
    """Read an integer field that may be missing, ``None`` or already numeric."""
    if value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def deepseek_token_buckets(
    *, prompt_tokens: int, cache_hit_tokens: int, cache_miss_tokens: int
) -> tuple[int, int]:
    """Return ``(input_tokens, cache_read_tokens)`` for DeepSeek usage.

    Gaia's base input bucket is DeepSeek's cache-*miss* count. When the provider
    omits the miss field, it is the prompt total minus cache hits; when it omits
    both cache fields, every prompt token is a miss.
    """
    miss = cache_miss_tokens
    hit = cache_hit_tokens
    if miss == 0 and hit:
        miss = max(0, prompt_tokens - hit)
    elif hit == 0 and miss == 0:
        miss = prompt_tokens
    return miss, hit


def _model(payload: Mapping[str, Any]) -> str:
    """Return the payload's model as a string, or ``""`` when it is absent."""
    model = payload.get("model")
    return "" if model is None else str(model)


def anthropic_usage_from_payload(
    payload: Mapping[str, Any], *, provider: str, request_id: str | None = None
) -> LLMUsage | None:
    """Parse one Anthropic JSON payload into an :class:`LLMUsage`, or ``None``."""
    usage = payload.get("usage")
    if not isinstance(usage, dict):
        return None
    return LLMUsage(
        provider=provider,
        model=_model(payload),
        input_tokens=_as_int(usage.get("input_tokens")),
        output_tokens=_as_int(usage.get("output_tokens")),
        cache_read_tokens=_as_int(usage.get("cache_read_input_tokens")),
        cache_creation_tokens=_as_int(usage.get("cache_creation_input_tokens")),
        request_id=request_id,
    )


def openai_usage_from_payload(
    payload: Mapping[str, Any], *, provider: str, request_id: str | None = None
) -> LLMUsage | None:
    """Parse one OpenAI-compatible JSON payload into an :class:`LLMUsage`."""
    usage = payload.get("usage")
    if not isinstance(usage, dict):
        return None
    prompt_tokens = _as_int(usage.get("prompt_tokens"))
    completion_tokens = _as_int(usage.get("completion_tokens"))
    cache_hit_tokens = _as_int(usage.get("prompt_cache_hit_tokens"))
    cache_miss_tokens = _as_int(usage.get("prompt_cache_miss_tokens"))
    input_tokens, cache_read_tokens = deepseek_token_buckets(
        prompt_tokens=prompt_tokens,
        cache_hit_tokens=cache_hit_tokens,
        cache_miss_tokens=cache_miss_tokens,
    )
    return LLMUsage(
        provider=provider,
        model=_model(payload),
        input_tokens=input_tokens,
        output_tokens=completion_tokens,
        cache_read_tokens=cache_read_tokens,
        cache_creation_tokens=0,
        request_id=request_id,
    )


class UsageAccumulator:
    """Accumulate relayed body bytes and produce one :class:`LLMUsage`.

    For non-streaming responses the body is buffered until :meth:`finish`. For
    streaming responses the SSE lines are parsed incrementally, so an event
    split across two chunks is still parsed correctly. A response with no
    recognisable usage makes :meth:`finish` return ``None``; the caller records
    an unmetered zero-token row.
    """

    def __init__(
        self, *, provider: str, parser: Literal["anthropic", "openai"], stream: bool
    ) -> None:
        self._provider = provider
        self._parser = parser
        self._stream = stream
        self._json_buffer = bytearray()
        self._sse_buffer = ""
        self._sse_event = ""
        self._anthropic_seen = False
        self._anthropic_model = ""
        self._anthropic_input_tokens = 0
        self._anthropic_output_tokens = 0
        self._anthropic_cache_read_tokens = 0
        self._anthropic_cache_creation_tokens = 0
        self._openai_payload: dict[str, Any] | None = None

    def feed(self, chunk: bytes) -> None:
        """Consume the next raw response chunk."""
        if self._stream:
            self._sse_buffer += chunk.decode("utf-8", errors="replace")
            self._process_sse(final=False)
        else:
            self._json_buffer.extend(chunk)

    def finish(self, *, request_id: str | None = None) -> LLMUsage | None:
        """Return the usage parsed so far, if any."""
        if self._parser == "anthropic":
            if self._stream:
                self._process_sse(final=True)
                if not self._anthropic_seen:
                    return None
                return LLMUsage(
                    provider=self._provider,
                    model=self._anthropic_model,
                    input_tokens=self._anthropic_input_tokens,
                    output_tokens=self._anthropic_output_tokens,
                    cache_read_tokens=self._anthropic_cache_read_tokens,
                    cache_creation_tokens=self._anthropic_cache_creation_tokens,
                    request_id=request_id,
                )
            payload = self._json_payload()
            if payload is None:
                return None
            return anthropic_usage_from_payload(
                payload, provider=self._provider, request_id=request_id
            )

        if self._stream:
            self._process_sse(final=True)
            if self._openai_payload is None:
                return None
            return openai_usage_from_payload(
                self._openai_payload, provider=self._provider, request_id=request_id
            )
        payload = self._json_payload()
        if payload is None:
            return None
        return openai_usage_from_payload(payload, provider=self._provider, request_id=request_id)

    def _json_payload(self) -> dict[str, Any] | None:
        """Parse the buffered non-streaming body as a JSON object."""
        try:
            payload = json.loads(self._json_buffer.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return None
        return payload if isinstance(payload, dict) else None

    def _process_sse(self, *, final: bool) -> None:
        """Split the SSE buffer into lines and parse complete ones."""
        while True:
            if not final and "\n" not in self._sse_buffer:
                return
            if "\n" in self._sse_buffer:
                line, self._sse_buffer = self._sse_buffer.split("\n", 1)
            else:
                line, self._sse_buffer = self._sse_buffer, ""
            self._handle_sse_line(line.rstrip("\r"))
            if final and not self._sse_buffer:
                return

    def _handle_sse_line(self, line: str) -> None:
        """Parse one SSE line; comments and empty lines are ignored."""
        if line.startswith(":"):
            return
        if not line:
            self._sse_event = ""
            return
        if line.startswith("event:"):
            self._sse_event = line[6:].strip()
            return
        if not line.startswith("data:"):
            return
        data = line[5:].strip()
        if not data:
            return
        if self._parser == "anthropic":
            self._handle_anthropic_data(data)
        else:
            self._handle_openai_data(data)

    def _handle_anthropic_data(self, data: str) -> None:
        """Parse one Anthropic SSE ``data:`` line."""
        if data == "[DONE]":
            return
        try:
            payload = json.loads(data)
        except json.JSONDecodeError:
            return
        if not isinstance(payload, dict):
            return
        event = self._sse_event
        if event == "message_start":
            message = payload.get("message")
            if not isinstance(message, dict):
                return
            self._anthropic_seen = True
            self._anthropic_model = _model(message)
            usage = message.get("usage")
            if isinstance(usage, dict):
                self._anthropic_input_tokens = _as_int(usage.get("input_tokens"))
                self._anthropic_output_tokens = _as_int(usage.get("output_tokens"))
                self._anthropic_cache_read_tokens = _as_int(usage.get("cache_read_input_tokens"))
                self._anthropic_cache_creation_tokens = _as_int(
                    usage.get("cache_creation_input_tokens")
                )
        elif event == "message_delta":
            usage = payload.get("usage")
            if isinstance(usage, dict) and usage.get("output_tokens") is not None:
                # Anthropic's delta output_tokens is cumulative: keep the last.
                self._anthropic_output_tokens = _as_int(usage.get("output_tokens"))

    def _handle_openai_data(self, data: str) -> None:
        """Keep the latest OpenAI-compatible SSE payload that carries usage."""
        if data == "[DONE]":
            return
        try:
            payload = json.loads(data)
        except json.JSONDecodeError:
            return
        if isinstance(payload, dict) and "usage" in payload:
            self._openai_payload = payload


#: Bytes per token used for a conservative estimate. Real tokenizers average about
#: 4 bytes per token on English text and code, so dividing by 3 over-counts on
#: purpose: an estimate must never let an agent spend more than its budget.
ESTIMATE_BYTES_PER_TOKEN = 3


def estimate_usage(
    *, provider: str, model: str, request_bytes: int, response_bytes: int
) -> LLMUsage:
    """A deliberately high usage estimate for a call that reported none.

    Used when a stream ends before the provider's usage block arrives, for
    example when the client disconnects or the upstream connection drops.
    DeepSeek reports usage only in the final chunk, so without this an
    interrupted stream would cost $0 and slip past the budget. Every input
    token is billed at the full (cache-miss) rate.
    """
    return LLMUsage(
        provider=provider,
        model=model,
        input_tokens=-(-request_bytes // ESTIMATE_BYTES_PER_TOKEN),
        output_tokens=-(-response_bytes // ESTIMATE_BYTES_PER_TOKEN),
    )
