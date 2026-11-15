"""A local, deterministic LLM provider for ``GAIA_FAKE_LLM=1`` (doc 18b-2).

The transport never opens a socket and never reads the ``Authorization`` or
``x-api-key`` header values. It answers Anthropic Messages requests and DeepSeek
chat-completion requests in the exact wire shapes the real SDK clients parse,
so a demo run exercises the gateway, metering, planning, review and the native
tool loop without a real model or a real key.
"""

from __future__ import annotations

import json
import shlex
import time
from collections.abc import AsyncIterator
from typing import Any

import httpx

from gaia.domain.plans import Plan, PlanStep, Review

#: Fixed token counts. Small enough to be cheap, non-zero so the dashboard shows
#: a real metered cost through the real price table.
INPUT_TOKENS = 800
OUTPUT_TOKENS = 200
DEEPSEEK_CACHE_HIT_TOKENS = 600
DEEPSEEK_CACHE_MISS_TOKENS = 200

#: The one demo plan the lead returns. Its step has no ``test_command``: the
#: plan validator allows that, and the check node falls back to the repository's
#: registered test command (lead.py and impl/checks.py).
DEMO_PLAN = Plan(
    summary="Add a short demo note to the repository.",
    risks=[],
    steps=[
        PlanStep(
            title="Add a demo note",
            instructions=(
                "Create GAIA_DEMO.md with a short note about the ticket. "
                "Keep the change small and do not touch other files."
            ),
            files_likely_touched=["GAIA_DEMO.md"],
            acceptance=["GAIA_DEMO.md exists and the repository tests pass."],
            test_command=None,
        )
    ],
)

DEMO_REVIEW = Review(
    verdict="accept",
    feedback="Demo review: the change matches the step.",
    issues=[],
)

DEMO_PLAN_JSON = DEMO_PLAN.model_dump_json()
DEMO_REVIEW_JSON = DEMO_REVIEW.model_dump_json()

ANTHROPIC_REQUEST_ID = "req_fake_anthropic"
DEEPSEEK_REQUEST_ID = "req_fake_deepseek"


def _json_bytes(payload: Any) -> bytes:
    """Encode ``payload`` as UTF-8 JSON."""
    return json.dumps(payload).encode("utf-8")


def _json_stream_response(
    status_code: int,
    payload: Any,
    *,
    headers: dict[str, str],
) -> httpx.Response:
    """A non-streaming JSON response, delivered through an async byte stream.

    The gateway always sends upstream with ``stream=True``. Returning ``content=``
    would make httpx mark the body as consumed before the gateway calls
    ``aiter_raw``; a one-chunk byte stream keeps the relay path identical for
    streaming and non-streaming provider responses.
    """
    return httpx.Response(
        status_code,
        headers=headers,
        stream=_ChunkedBody([_json_bytes(payload)]),
    )


def _sse(event: str, payload: Any) -> bytes:
    """One SSE event with the ``event:`` field the SDK and gateway read."""
    data = json.dumps(payload, separators=(",", ":"))
    return f"event: {event}\ndata: {data}\n\n".encode()


class _ChunkedBody(httpx.AsyncByteStream):
    """An async response body that yields fixed bytes chunks."""

    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = chunks

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for chunk in self._chunks:
            yield chunk


class FakeProviderTransport(httpx.AsyncBaseTransport):
    """An ``httpx.AsyncBaseTransport`` that plays the role of both providers."""

    async def aclose(self) -> None:
        """Nothing to close for an in-memory transport."""

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        """Return a provider-shaped response for ``request``."""
        raw = await request.aread()
        path = request.url.path
        try:
            payload = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return self._provider_error(path, 400, "invalid_request_error", "invalid JSON body")
        if not isinstance(payload, dict):
            return self._provider_error(
                path, 400, "invalid_request_error", "request body must be a JSON object"
            )

        if path.endswith("/v1/messages/count_tokens"):
            return self._anthropic_count_tokens(payload)
        if path.endswith("/chat/completions") or path.endswith("/v1/chat/completions"):
            return self._deepseek(payload)
        if path.endswith("/v1/messages") or path.endswith("/anthropic/v1/messages"):
            return self._anthropic(payload)
        return self._provider_error(
            path, 400, "invalid_request_error", "the requested fake endpoint is not supported"
        )

    # ------------------------------------------------------------ Anthropic --

    def _anthropic(self, payload: dict[str, Any]) -> httpx.Response:
        """Answer an Anthropic Messages request, streamed or not."""
        model = _model(payload)
        text = self._anthropic_text(payload)
        stream = payload.get("stream") is True or "output_format" in payload
        if stream:
            return self._anthropic_stream(model, text)
        return self._anthropic_json(model, text)

    def _anthropic_count_tokens(self, payload: dict[str, Any]) -> httpx.Response:
        """Answer the un-metered count_tokens endpoint."""
        del payload
        return _json_stream_response(
            200,
            {"input_tokens": INPUT_TOKENS},
            headers={
                "content-type": "application/json",
                "request-id": ANTHROPIC_REQUEST_ID,
            },
        )

    def _anthropic_text(self, payload: dict[str, Any]) -> str:
        """The text content this request should carry."""
        if _schema_contains(payload, "verdict"):
            return DEMO_REVIEW_JSON
        if _schema_contains(payload, "steps") and _schema_contains(payload, "summary"):
            return DEMO_PLAN_JSON
        if "output_format" in payload:
            return '{"message":"demo structured response"}'
        return "demo response"

    def _anthropic_stream(self, model: str, text: str) -> httpx.Response:
        """One Anthropic SSE stream ending in a parsed final message."""
        message_start: dict[str, Any] = {
            "id": "msg_demo",
            "type": "message",
            "role": "assistant",
            "model": model,
            "content": [],
            "stop_reason": None,
            "stop_sequence": None,
            "usage": {
                "input_tokens": INPUT_TOKENS,
                "output_tokens": 0,
                "cache_read_input_tokens": 0,
                "cache_creation_input_tokens": 0,
            },
        }
        chunks = [
            _sse("message_start", {"type": "message_start", "message": message_start}),
            _sse(
                "content_block_start",
                {
                    "type": "content_block_start",
                    "index": 0,
                    "content_block": {"type": "text", "text": ""},
                },
            ),
            _sse(
                "content_block_delta",
                {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "text_delta", "text": text},
                },
            ),
            _sse("content_block_stop", {"type": "content_block_stop", "index": 0}),
            _sse(
                "message_delta",
                {
                    "type": "message_delta",
                    "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                    "usage": {"output_tokens": OUTPUT_TOKENS},
                },
            ),
            _sse("message_stop", {"type": "message_stop"}),
        ]
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream", "request-id": ANTHROPIC_REQUEST_ID},
            stream=_ChunkedBody(chunks),
        )

    def _anthropic_json(self, model: str, text: str) -> httpx.Response:
        """One non-streaming Anthropic Messages response."""
        message = {
            "id": "msg_demo",
            "type": "message",
            "role": "assistant",
            "model": model,
            "content": [{"type": "text", "text": text}],
            "stop_reason": "end_turn",
            "stop_sequence": None,
            "usage": {
                "input_tokens": INPUT_TOKENS,
                "output_tokens": OUTPUT_TOKENS,
                "cache_read_input_tokens": 0,
                "cache_creation_input_tokens": 0,
            },
        }
        return _json_stream_response(
            200,
            message,
            headers={"content-type": "application/json", "request-id": ANTHROPIC_REQUEST_ID},
        )

    # ------------------------------------------------------------- DeepSeek --

    def _deepseek(self, payload: dict[str, Any]) -> httpx.Response:
        """Answer an OpenAI-compatible chat completion."""
        model = _model(payload)
        if payload.get("stream") is True:
            return self._deepseek_stream(model, self._deepseek_text(payload))
        return self._deepseek_json(payload, model)

    def _deepseek_text(self, payload: dict[str, Any]) -> str:
        """The text for a non-tool, non-streaming DeepSeek completion."""
        if _is_deepseek_parse(payload):
            system = _system_prompt(payload)
            if '"verdict"' in system:
                return DEMO_REVIEW_JSON
            if '"steps"' in system and '"summary"' in system:
                return DEMO_PLAN_JSON
            return '{"message":"demo structured response"}'
        return "demo response"

    def _deepseek_json(self, payload: dict[str, Any], model: str) -> httpx.Response:
        """One non-streaming DeepSeek completion."""
        if payload.get("tools"):
            return self._deepseek_tool_response(payload, model)
        content = self._deepseek_text(payload)
        return _openai_json_response(
            model=model,
            content=content,
            reasoning_content="demo reasoning",
            finish_reason="stop",
            request_id=DEEPSEEK_REQUEST_ID,
        )

    def _deepseek_tool_response(self, payload: dict[str, Any], model: str) -> httpx.Response:
        """One tool-loop turn for the native DeepSeek runtime."""
        messages = payload.get("messages")
        if not isinstance(messages, list):
            return _openai_json_response(
                model=model,
                content="demo response",
                reasoning_content="demo reasoning",
                finish_reason="stop",
                request_id=DEEPSEEK_REQUEST_ID,
            )

        assistant_turns = _assistant_turns(messages)
        first_user = _first_user_message(messages)
        title = _title_from_instructions(first_user)
        test_command = _test_command_from_instructions(first_user)

        if assistant_turns == 0:
            note = f"Demo note for {title}.\n"
            return _deepseek_tool_call_response(
                model,
                "call_1",
                "write_file",
                {"path": "GAIA_DEMO.md", "content": note},
                request_id=DEEPSEEK_REQUEST_ID,
            )

        if assistant_turns == 1 and test_command:
            return _deepseek_tool_call_response(
                model,
                "call_2",
                "run_command",
                {"argv": test_command},
                request_id=DEEPSEEK_REQUEST_ID,
            )

        # Turn 3 (or turn 2 when the task text does not name a test command):
        # finish. tests_passed is true only when the latest tool result is a
        # command that exited 0.
        passed = bool(test_command) and _latest_command_passed(messages)
        finish_args = {
            "summary": f"Created GAIA_DEMO.md with a short note for {title}.",
            "tests_run": [shlex.join(test_command)] if test_command else [],
            "tests_passed": passed,
            "concerns": [],
        }
        return _deepseek_tool_call_response(
            model,
            "call_3",
            "finish",
            finish_args,
            request_id=DEEPSEEK_REQUEST_ID,
        )

    def _deepseek_stream(self, model: str, text: str) -> httpx.Response:
        """One OpenAI-compatible SSE stream with a final usage chunk."""
        chunks = [
            _sse(
                "message",
                {
                    "id": "chatcmpl_demo",
                    "object": "chat.completion.chunk",
                    "created": int(time.time()),
                    "model": model,
                    "choices": [
                        {
                            "index": 0,
                            "delta": {"role": "assistant", "content": ""},
                            "finish_reason": None,
                        }
                    ],
                },
            ),
            _sse(
                "message",
                {
                    "id": "chatcmpl_demo",
                    "object": "chat.completion.chunk",
                    "created": int(time.time()),
                    "model": model,
                    "choices": [{"index": 0, "delta": {"content": text}, "finish_reason": None}],
                },
            ),
            _sse(
                "message",
                {
                    "id": "chatcmpl_demo",
                    "object": "chat.completion.chunk",
                    "created": int(time.time()),
                    "model": model,
                    "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                    "usage": _deepseek_usage(),
                },
            ),
            b"data: [DONE]\n\n",
        ]
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream", "request-id": DEEPSEEK_REQUEST_ID},
            stream=_ChunkedBody(chunks),
        )

    def _provider_error(
        self,
        path: str,
        status_code: int,
        error_type: str,
        message: str,
    ) -> httpx.Response:
        """A provider-shaped error, loud enough to fail a mismatch."""
        body: dict[str, Any]
        if "deepseek" in path or path.endswith("/chat/completions"):
            body = {
                "error": {
                    "message": message,
                    "type": error_type,
                    "code": error_type,
                }
            }
        else:
            body = {
                "type": "error",
                "error": {"type": error_type, "message": message},
            }
        return _json_stream_response(
            status_code,
            body,
            headers={"content-type": "application/json", "request-id": "req_fake_error"},
        )


def _model(payload: dict[str, Any]) -> str:
    """Return the request model, defaulting to an unpriced but valid-looking name."""
    model = payload.get("model")
    return model if isinstance(model, str) and model else "unknown"


def _schema_contains(payload: dict[str, Any], key: str) -> bool:
    """True when the Anthropic structured-output schema mentions ``key``.

    The SDK may put the schema in the top-level ``output_format`` field (older
    clients) or inside ``output_config.format.schema`` (anthropic 2.8.0, the
    installed version).
    """
    candidates: list[Any] = []
    output_format = payload.get("output_format")
    if isinstance(output_format, dict):
        candidates.append(output_format)
    output_config = payload.get("output_config")
    if isinstance(output_config, dict):
        format_value = output_config.get("format")
        if isinstance(format_value, dict):
            schema = format_value.get("schema")
            if isinstance(schema, dict):
                candidates.append(schema)
        candidates.append(output_config)
    return any(f'"{key}"' in json.dumps(candidate, sort_keys=True) for candidate in candidates)


def _is_deepseek_parse(payload: dict[str, Any]) -> bool:
    """True for DeepSeek JSON-mode parse calls."""
    return payload.get("response_format") == {"type": "json_object"}


def _system_prompt(payload: dict[str, Any]) -> str:
    """The system message text for a DeepSeek request."""
    messages = payload.get("messages")
    if not isinstance(messages, list):
        return ""
    for message in messages:
        if isinstance(message, dict) and message.get("role") == "system":
            return str(message.get("content") or "")
    return ""


def _assistant_turns(messages: list[Any]) -> int:
    """The number of assistant messages already in the conversation."""
    return sum(
        1
        for message in messages
        if isinstance(message, dict) and message.get("role") == "assistant"
    )


def _first_user_message(messages: list[Any]) -> str:
    """The first user message content, if any."""
    for message in messages:
        if isinstance(message, dict) and message.get("role") == "user":
            return str(message.get("content") or "")
    return ""


def _title_from_instructions(instructions: str) -> str:
    """Read the plan-step title from the native runtime's first user message."""
    for line in instructions.splitlines():
        stripped = line.strip()
        if stripped.startswith("Title:"):
            title = stripped.removeprefix("Title:").strip()
            if title:
                return title
    first_line = next(
        (
            line.strip()
            for line in instructions.splitlines()
            if line.strip() and not line.strip().startswith("Title:")
        ),
        "",
    )
    return first_line[:200] or "demo ticket"


def _test_command_from_instructions(instructions: str) -> list[str] | None:
    """Extract the step's test command, or ``None`` when it has none."""
    marker = '<untrusted_content source="plan:test-command">'
    if marker in instructions:
        after = instructions.split(marker, 1)[1]
        text = after.split("</untrusted_content>", 1)[0].strip()
    else:
        # ``gaia dev run-task`` passes free-form instructions; accept a
        # ``Test command:`` line as the fallback.
        text = ""
        for line in instructions.splitlines():
            if line.strip().startswith("Test command:"):
                text = line.strip().removeprefix("Test command:").strip()
                break
    if not text:
        return None
    try:
        argv = shlex.split(text)
    except ValueError:
        return None
    return argv or None


def _latest_command_passed(messages: list[Any]) -> bool:
    """True when the latest tool result starts with ``exit=0``."""
    for message in reversed(messages):
        if isinstance(message, dict) and message.get("role") == "tool":
            return str(message.get("content") or "").startswith("exit=0")
    return False


def _deepseek_usage() -> dict[str, int]:
    """Fixed DeepSeek usage with cache buckets the gateway can price."""
    return {
        "prompt_tokens": INPUT_TOKENS,
        "completion_tokens": OUTPUT_TOKENS,
        "prompt_cache_hit_tokens": DEEPSEEK_CACHE_HIT_TOKENS,
        "prompt_cache_miss_tokens": DEEPSEEK_CACHE_MISS_TOKENS,
    }


def _deepseek_tool_call_response(
    model: str,
    call_id: str,
    name: str,
    arguments: dict[str, Any],
    *,
    request_id: str,
) -> httpx.Response:
    """One OpenAI tool-call completion for the native runtime."""
    body = {
        "id": "chatcmpl_demo",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": None,
                    "reasoning_content": "demo reasoning",
                    "tool_calls": [
                        {
                            "id": call_id,
                            "type": "function",
                            "function": {
                                "name": name,
                                "arguments": json.dumps(arguments),
                            },
                        }
                    ],
                },
                "finish_reason": "tool_calls",
            }
        ],
        "usage": _deepseek_usage(),
    }
    return _json_stream_response(
        200,
        body,
        headers={"content-type": "application/json", "request-id": request_id},
    )


def _openai_json_response(
    *,
    model: str,
    content: str,
    reasoning_content: str,
    finish_reason: str,
    request_id: str,
) -> httpx.Response:
    """One non-streaming OpenAI completion with a single assistant message."""
    body = {
        "id": "chatcmpl_demo",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": content,
                    "reasoning_content": reasoning_content,
                },
                "finish_reason": finish_reason,
            }
        ],
        "usage": _deepseek_usage(),
    }
    return _json_stream_response(
        200,
        body,
        headers={"content-type": "application/json", "request-id": request_id},
    )
