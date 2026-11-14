"""Anthropic client: request shape, fallbacks, streaming and stop handling."""

from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import anthropic
import httpx2
import pytest
from pydantic import SecretStr

from gaia.llm.anthropic_client import AnthropicClient
from gaia.llm.base import LLMMessage, LLMStop, LLMTool, LLMUsage
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


class StubStream:
    def __init__(self, message: Any, error: Exception | None = None) -> None:
        self._message = message
        self._error = error

    async def __aenter__(self) -> StubStream:
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        return None

    async def get_final_message(self) -> Any:
        if self._error is not None:
            raise self._error
        return self._message


class StubMessages:
    def __init__(self, message: Any, error: Exception | None = None) -> None:
        self._message = message
        self._error = error
        self.create_calls: list[dict[str, Any]] = []
        self.stream_calls: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> Any:
        self.create_calls.append(kwargs)
        if self._error is not None:
            raise self._error
        return self._message

    def stream(self, **kwargs: Any) -> StubStream:
        self.stream_calls.append(kwargs)
        return StubStream(self._message, self._error)


class StubAnthropic:
    def __init__(self, message: Any, error: Exception | None = None) -> None:
        self.messages = StubMessages(message, error)
        self.beta = SimpleNamespace(messages=StubMessages(message, error))


def _price_table() -> PriceTable:
    return PriceTable(
        {
            "claude-opus-5-5": ModelPrice(
                rates=PriceRates(
                    input=Decimal("4"),
                    output=Decimal("20"),
                    cache_read=Decimal("0.2"),
                    cache_write=Decimal("5"),
                )
            )
        }
    )


def _client(
    stub: Any,
    recorder: RecordingRecorder,
    *,
    model: str = "claude-opus-5-5",
) -> AnthropicClient:
    return AnthropicClient(
        api_key=SecretStr("sk-ant-test-1234567890"),
        model=model,
        recorder=recorder,
        price_table=_price_table(),
        client=stub,
    )


def _message(**overrides: Any) -> Any:
    values: dict[str, Any] = {
        "model": "claude-opus-5-5",
        "stop_reason": "end_turn",
        "content": [SimpleNamespace(type="text", text="hi")],
        "usage": SimpleNamespace(
            input_tokens=100,
            output_tokens=50,
            cache_read_input_tokens=10,
            cache_creation_input_tokens=20,
        ),
        "_request_id": "req_1",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _anthropic_error(kind: str) -> Exception:
    request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    if kind == "rate":
        return anthropic.RateLimitError(
            "slow down",
            response=httpx2.Response(429, request=request, headers={"request-id": "req_rate"}),
            body=None,
        )
    if kind == "status":
        return anthropic.APIStatusError(
            "bad request",
            response=httpx2.Response(400, request=request, headers={"request-id": "req_status"}),
            body=None,
        )
    return anthropic.APIConnectionError(request=request)


async def test_constructor_builds_sdk_client_with_explicit_key() -> None:
    client = AnthropicClient(
        api_key=SecretStr("sk-ant-test-1234567890"),
        model="claude-opus-5-5",
        recorder=RecordingRecorder(),
        price_table=_price_table(),
    )
    assert client._client is not None


async def test_non_streaming_request_shape_and_recording() -> None:
    stub = StubAnthropic(_message())
    recorder = RecordingRecorder()
    client = _client(stub, recorder)

    response = await client.complete(
        system="be brief",
        messages=[LLMMessage(role="user", content="hello")],
        tools=[LLMTool(name="read", description="read a file", input_schema={"type": "object"})],
        max_tokens=100,
    )

    assert stub.messages.create_calls
    assert not stub.messages.stream_calls
    kwargs = stub.messages.create_calls[0]
    assert kwargs["model"] == "claude-opus-5-5"
    assert kwargs["max_tokens"] == 100
    assert kwargs["system"] == "be brief"
    assert kwargs["messages"] == [{"role": "user", "content": "hello"}]
    assert kwargs["output_config"] == {"effort": "high"}
    assert kwargs["tool_choice"] == {"type": "auto"}
    assert kwargs["cache_control"] == {"type": "ephemeral"}
    assert kwargs["tools"][0]["name"] == "read"
    assert kwargs["tools"][0]["strict"] is True
    assert "thinking" not in kwargs
    assert "temperature" not in kwargs
    assert "top_p" not in kwargs
    assert "top_k" not in kwargs

    assert response.text == "hi"
    assert response.stop_reason == "end_turn"
    assert response.request_id == "req_1"
    assert len(recorder.records) == 1
    usage, cost, tier = recorder.records[0]
    assert usage.input_tokens == 100
    assert usage.output_tokens == 50
    assert usage.cache_read_tokens == 10
    assert usage.cache_creation_tokens == 20
    assert cost == Decimal("0.001502")
    assert tier is None


async def test_assistant_messages_are_converted() -> None:
    stub = StubAnthropic(_message())
    client = _client(stub, RecordingRecorder())

    await client.complete(
        system="sys",
        messages=[
            LLMMessage(role="user", content="hello"),
            LLMMessage(role="assistant", content="hi there"),
        ],
    )

    assert stub.messages.create_calls[0]["messages"] == [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "hi there"},
    ]


async def test_empty_system_omits_cache_control_and_tools() -> None:
    stub = StubAnthropic(_message())
    client = _client(stub, RecordingRecorder())

    await client.complete(system="", messages=[LLMMessage(role="user", content="hello")])

    kwargs = stub.messages.create_calls[0]
    assert "cache_control" not in kwargs
    assert "tools" not in kwargs


async def test_large_max_tokens_streams() -> None:
    stub = StubAnthropic(_message())
    client = _client(stub, RecordingRecorder())

    await client.complete(
        system="sys",
        messages=[LLMMessage(role="user", content="hello")],
        max_tokens=8193,
    )

    assert not stub.messages.create_calls
    assert stub.messages.stream_calls
    assert stub.messages.stream_calls[0]["max_tokens"] == 8193


async def test_fallback_uses_beta_create_with_betas_and_fallbacks() -> None:
    stub = StubAnthropic(_message())
    client = _client(stub, RecordingRecorder())

    await client.complete(
        system="sys",
        messages=[LLMMessage(role="user", content="hello")],
        enable_fallback=True,
        max_tokens=100,
    )

    assert not stub.messages.create_calls
    assert stub.beta.messages.create_calls
    kwargs = stub.beta.messages.create_calls[0]
    assert kwargs["betas"] == ["server-side-fallback-2026-07-01"]
    assert kwargs["fallbacks"] == "default"


async def test_fallback_streams_large_max_tokens() -> None:
    stub = StubAnthropic(_message())
    client = _client(stub, RecordingRecorder())

    await client.complete(
        system="sys",
        messages=[LLMMessage(role="user", content="hello")],
        enable_fallback=True,
        max_tokens=8193,
    )

    assert not stub.beta.messages.create_calls
    assert stub.beta.messages.stream_calls
    assert stub.beta.messages.stream_calls[0]["fallbacks"] == "default"


async def test_tool_use_blocks_are_returned() -> None:
    message = _message(
        stop_reason="tool_use",
        content=[
            SimpleNamespace(type="text", text="calling"),
            SimpleNamespace(type="tool_use", id="tool_1", name="read", input={"path": "a.py"}),
        ],
    )
    stub = StubAnthropic(message)
    client = _client(stub, RecordingRecorder())

    response = await client.complete(system="sys", messages=[LLMMessage(role="user", content="go")])

    assert response.text == "calling"
    assert response.stop_reason == "tool_use"
    assert len(response.tool_calls) == 1
    assert response.tool_calls[0].id == "tool_1"
    assert response.tool_calls[0].name == "read"
    assert response.tool_calls[0].arguments == {"path": "a.py"}


async def test_refusal_stop_raises_after_recording() -> None:
    message = _message(
        stop_reason="refusal",
        content=[],
        stop_details=SimpleNamespace(category="cyber"),
    )
    stub = StubAnthropic(message)
    recorder = RecordingRecorder()
    client = _client(stub, recorder)

    with pytest.raises(LLMStop) as caught:
        await client.complete(system="sys", messages=[LLMMessage(role="user", content="go")])

    assert caught.value.stop_reason == "refusal"
    assert caught.value.refusal_category == "cyber"
    assert caught.value.request_id == "req_1"
    assert len(recorder.records) == 1


async def test_max_tokens_stop_raises_after_recording() -> None:
    stub = StubAnthropic(_message(stop_reason="max_tokens", content=[]))
    recorder = RecordingRecorder()
    client = _client(stub, recorder)

    with pytest.raises(LLMStop) as caught:
        await client.complete(system="sys", messages=[LLMMessage(role="user", content="go")])

    assert caught.value.stop_reason == "max_tokens"
    assert caught.value.refusal_category is None
    assert len(recorder.records) == 1


async def test_pause_turn_stop_raises() -> None:
    stub = StubAnthropic(_message(stop_reason="pause_turn", content=[]))
    client = _client(stub, RecordingRecorder())

    with pytest.raises(LLMStop) as caught:
        await client.complete(system="sys", messages=[LLMMessage(role="user", content="go")])

    assert caught.value.stop_reason == "pause_turn"


async def test_missing_usage_fields_default_to_zero() -> None:
    message = SimpleNamespace(
        model="claude-opus-5-5",
        stop_reason="end_turn",
        content=[SimpleNamespace(type="text", text="ok")],
        usage=SimpleNamespace(input_tokens=None, output_tokens=None),
        _request_id="req_no_usage",
    )
    stub = StubAnthropic(message)
    recorder = RecordingRecorder()
    client = _client(stub, recorder)

    response = await client.complete(system="sys", messages=[LLMMessage(role="user", content="go")])

    assert response.usage.input_tokens == 0
    assert response.usage.output_tokens == 0
    assert recorder.records[0][1] == Decimal("0")


async def test_system_role_message_is_rejected() -> None:
    stub = StubAnthropic(_message())
    client = _client(stub, RecordingRecorder())

    with pytest.raises(ValueError, match="user/assistant"):
        await client.complete(
            system="sys",
            messages=[LLMMessage(role="system", content="nope")],
        )


@pytest.mark.parametrize("kind", ["rate", "status", "connection"])
async def test_sdk_errors_are_reraises(kind: str) -> None:
    stub = StubAnthropic(_message(), error=_anthropic_error(kind))
    client = _client(stub, RecordingRecorder())

    with pytest.raises(
        (anthropic.RateLimitError, anthropic.APIStatusError, anthropic.APIConnectionError)
    ):
        await client.complete(system="sys", messages=[LLMMessage(role="user", content="go")])


async def test_tool_choice_is_sent_only_with_tools() -> None:
    # The API answers 400 when tool_choice arrives without tools (lead review).
    stub = StubAnthropic(_message())
    client = _client(stub, RecordingRecorder())

    await client.complete(system="sys", messages=[LLMMessage(role="user", content="hello")])
    await client.complete(
        system="sys",
        messages=[LLMMessage(role="user", content="hello")],
        tools=[LLMTool(name="t", description="d", input_schema={"type": "object"})],
    )

    without_tools, with_tools = stub.messages.create_calls
    assert "tool_choice" not in without_tools
    assert with_tools["tool_choice"] == {"type": "auto"}


async def test_a_fallback_model_without_a_price_is_still_recorded() -> None:
    # A refusal fallback can be served by a model prices.toml does not list.
    recorder = RecordingRecorder()
    stub = StubAnthropic(_message(model="claude-opus-4-8"))
    client = _client(stub, recorder)

    response = await client.complete(
        system="sys", messages=[LLMMessage(role="user", content="hello")], enable_fallback=True
    )

    usage, cost, tier = recorder.records[0]
    assert usage.model == "claude-opus-4-8" == response.model
    assert tier == "priced-as-requested"
    assert cost > 0


def test_a_model_without_a_price_is_refused_before_any_call() -> None:
    with pytest.raises(KeyError, match="no price configured"):
        _client(StubAnthropic(_message()), RecordingRecorder(), model="claude-unknown")
