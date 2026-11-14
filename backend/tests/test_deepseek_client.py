"""DeepSeek client: OpenAI-compatible request shape and defensive usage reads."""

from __future__ import annotations

import json
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import httpx2
import openai
import pytest
from pydantic import SecretStr

from gaia.llm.base import LLMMessage, LLMStop, LLMTool, LLMUsage, ToolCall
from gaia.llm.deepseek_client import DeepSeekClient
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


class StubCompletions:
    def __init__(self, completion: Any, error: Exception | None = None) -> None:
        self._completion = completion
        self._error = error
        self.create_calls: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> Any:
        self.create_calls.append(kwargs)
        if self._error is not None:
            raise self._error
        return self._completion


class StubDeepSeek:
    def __init__(self, completion: Any, error: Exception | None = None) -> None:
        self.chat = SimpleNamespace(completions=StubCompletions(completion, error))


def _price_table() -> PriceTable:
    return PriceTable(
        {
            "deepseek-v4-pro": ModelPrice(
                rates=PriceRates(
                    input=Decimal("1"),
                    output=Decimal("2"),
                    cache_read=Decimal("0.5"),
                )
            )
        }
    )


def _client(stub: Any, recorder: RecordingRecorder) -> DeepSeekClient:
    return DeepSeekClient(
        api_key=SecretStr("sk-deepseek-test-1234567890"),
        model="deepseek-v4-pro",
        base_url="https://api.deepseek.com",
        recorder=recorder,
        price_table=_price_table(),
        client=stub,
    )


def _completion(**overrides: Any) -> Any:
    values: dict[str, Any] = {
        "id": "chatcmpl_1",
        "model": "deepseek-v4-pro",
        "choices": [
            SimpleNamespace(
                finish_reason="stop",
                message=SimpleNamespace(content="hi", tool_calls=None),
            )
        ],
        "usage": SimpleNamespace(
            prompt_tokens=1000,
            completion_tokens=500,
            prompt_cache_hit_tokens=200,
            prompt_cache_miss_tokens=800,
        ),
        "_request_id": "req_ds_1",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _openai_error(kind: str) -> Exception:
    request = httpx2.Request("POST", "https://api.deepseek.com/chat/completions")
    if kind == "rate":
        return openai.RateLimitError(
            "slow down",
            response=httpx2.Response(429, request=request, headers={"x-request-id": "req_rate_ds"}),
            body=None,
        )
    if kind == "status":
        return openai.APIStatusError(
            "bad request",
            response=httpx2.Response(
                400, request=request, headers={"x-request-id": "req_status_ds"}
            ),
            body=None,
        )
    return openai.APIConnectionError(message="down", request=request)


async def test_constructor_builds_sdk_client_with_explicit_key() -> None:
    client = DeepSeekClient(
        api_key=SecretStr("sk-deepseek-test-1234567890"),
        model="deepseek-v4-pro",
        base_url="https://api.deepseek.com",
        recorder=RecordingRecorder(),
        price_table=_price_table(),
    )
    assert client._client is not None


async def test_request_shape_and_cost_mapping() -> None:
    stub = StubDeepSeek(_completion())
    recorder = RecordingRecorder()
    client = _client(stub, recorder)

    response = await client.complete(
        messages=[
            LLMMessage(role="system", content="sys"),
            LLMMessage(role="user", content="hello"),
            LLMMessage(
                role="assistant",
                content="",
                reasoning_content="thinking",
                tool_calls=(ToolCall(id="call_1", name="read", arguments={"path": "x"}),),
            ),
            LLMMessage(role="tool", content="file", tool_call_id="call_1"),
        ],
        tools=[LLMTool(name="read", description="read a file", input_schema={"type": "object"})],
        max_tokens=512,
    )

    kwargs = stub.chat.completions.create_calls[0]
    assert kwargs["model"] == "deepseek-v4-pro"
    assert kwargs["max_tokens"] == 512
    assert kwargs["messages"][0] == {"role": "system", "content": "sys"}
    assistant = kwargs["messages"][2]
    assert assistant["role"] == "assistant"
    assert assistant["reasoning_content"] == "thinking"
    assert assistant["tool_calls"] == [
        {
            "id": "call_1",
            "type": "function",
            "function": {"name": "read", "arguments": json.dumps({"path": "x"})},
        }
    ]
    assert kwargs["messages"][3] == {
        "role": "tool",
        "tool_call_id": "call_1",
        "content": "file",
    }
    assert kwargs["tools"][0]["type"] == "function"
    assert kwargs["tools"][0]["function"]["parameters"] == {"type": "object"}

    assert response.text == "hi"
    assert response.stop_reason == "stop"
    assert response.request_id == "req_ds_1"
    usage, cost, tier = recorder.records[0]
    assert usage.input_tokens == 800
    assert usage.output_tokens == 500
    assert usage.cache_read_tokens == 200
    assert usage.cache_creation_tokens == 0
    assert cost == Decimal("0.0019")
    assert tier is None


async def test_invalid_message_role_is_rejected() -> None:
    stub = StubDeepSeek(_completion())
    client = _client(stub, RecordingRecorder())
    invalid = LLMMessage(role="invalid", content="x")  # type: ignore[arg-type]  # runtime guard

    with pytest.raises(ValueError, match="unsupported DeepSeek message role"):
        await client.complete(messages=[invalid])


async def test_plain_assistant_message_has_no_reasoning_or_tool_calls() -> None:
    stub = StubDeepSeek(_completion())
    client = _client(stub, RecordingRecorder())

    await client.complete(
        messages=[
            LLMMessage(role="user", content="hello"),
            LLMMessage(role="assistant", content="hi there"),
        ]
    )

    messages = stub.chat.completions.create_calls[0]["messages"]
    assert messages[1] == {"role": "assistant", "content": "hi there"}


async def test_tool_message_can_be_followed_by_another_message() -> None:
    stub = StubDeepSeek(_completion())
    client = _client(stub, RecordingRecorder())

    await client.complete(
        messages=[
            LLMMessage(role="tool", content="file", tool_call_id="call_1"),
            LLMMessage(role="user", content="again"),
        ]
    )

    messages = stub.chat.completions.create_calls[0]["messages"]
    assert messages == [
        {"role": "tool", "tool_call_id": "call_1", "content": "file"},
        {"role": "user", "content": "again"},
    ]


async def test_usage_fields_can_arrive_in_model_extra() -> None:
    completion = _completion(
        usage=SimpleNamespace(
            prompt_tokens=1000,
            completion_tokens=500,
            model_extra={"prompt_cache_hit_tokens": 300, "prompt_cache_miss_tokens": 700},
        )
    )
    stub = StubDeepSeek(completion)
    recorder = RecordingRecorder()
    client = _client(stub, recorder)

    await client.complete(messages=[LLMMessage(role="user", content="hello")])

    usage = recorder.records[0][0]
    assert usage.input_tokens == 700
    assert usage.cache_read_tokens == 300


async def test_missing_cache_miss_is_derived_from_prompt_tokens() -> None:
    completion = _completion(
        usage=SimpleNamespace(
            prompt_tokens=1000, completion_tokens=500, prompt_cache_hit_tokens=200
        )
    )
    stub = StubDeepSeek(completion)
    recorder = RecordingRecorder()
    client = _client(stub, recorder)

    await client.complete(messages=[LLMMessage(role="user", content="hello")])

    usage = recorder.records[0][0]
    assert usage.input_tokens == 800
    assert usage.cache_read_tokens == 200


async def test_missing_both_cache_fields_treats_prompt_as_miss() -> None:
    completion = _completion(usage=SimpleNamespace(prompt_tokens=1000, completion_tokens=500))
    stub = StubDeepSeek(completion)
    recorder = RecordingRecorder()
    client = _client(stub, recorder)

    await client.complete(messages=[LLMMessage(role="user", content="hello")])

    usage = recorder.records[0][0]
    assert usage.input_tokens == 1000
    assert usage.cache_read_tokens == 0


async def test_length_finish_raises_after_recording() -> None:
    completion = _completion(
        choices=[
            SimpleNamespace(
                finish_reason="length", message=SimpleNamespace(content="", tool_calls=None)
            )
        ]
    )
    stub = StubDeepSeek(completion)
    recorder = RecordingRecorder()
    client = _client(stub, recorder)

    with pytest.raises(LLMStop) as caught:
        await client.complete(messages=[LLMMessage(role="user", content="hello")])

    assert caught.value.stop_reason == "max_tokens"
    assert caught.value.request_id == "req_ds_1"
    assert len(recorder.records) == 1


async def test_content_filter_finish_raises_after_recording() -> None:
    completion = _completion(
        choices=[
            SimpleNamespace(
                finish_reason="content_filter",
                message=SimpleNamespace(content="", tool_calls=None),
            )
        ]
    )
    stub = StubDeepSeek(completion)
    recorder = RecordingRecorder()
    client = _client(stub, recorder)

    with pytest.raises(LLMStop) as caught:
        await client.complete(messages=[LLMMessage(role="user", content="hello")])

    assert caught.value.stop_reason == "refusal"
    assert caught.value.refusal_category == "content_filter"
    assert len(recorder.records) == 1


async def test_tool_calls_are_parsed_from_openai_function_calls() -> None:
    completion = _completion(
        choices=[
            SimpleNamespace(
                finish_reason="tool_calls",
                message=SimpleNamespace(
                    content="",
                    tool_calls=[
                        SimpleNamespace(
                            id="call_1",
                            type="function",
                            function=SimpleNamespace(name="read", arguments='{"path": "x"}'),
                        )
                    ],
                ),
            )
        ]
    )
    stub = StubDeepSeek(completion)
    client = _client(stub, RecordingRecorder())

    response = await client.complete(messages=[LLMMessage(role="user", content="hello")])

    assert response.stop_reason == "tool_calls"
    assert response.tool_calls == (ToolCall(id="call_1", name="read", arguments={"path": "x"}),)


@pytest.mark.parametrize("arguments", ["not json", "[1, 2]"])
async def test_invalid_tool_call_arguments_become_empty_dict(arguments: str) -> None:
    completion = _completion(
        choices=[
            SimpleNamespace(
                finish_reason="tool_calls",
                message=SimpleNamespace(
                    content="",
                    tool_calls=[
                        SimpleNamespace(
                            id="call_bad",
                            type="function",
                            function=SimpleNamespace(name="read", arguments=arguments),
                        )
                    ],
                ),
            )
        ]
    )
    stub = StubDeepSeek(completion)
    client = _client(stub, RecordingRecorder())

    response = await client.complete(messages=[LLMMessage(role="user", content="hello")])

    assert response.tool_calls[0].arguments == {}


async def test_missing_usage_object_defaults_to_zero_tokens() -> None:
    completion = _completion(usage=None)
    stub = StubDeepSeek(completion)
    recorder = RecordingRecorder()
    client = _client(stub, recorder)

    response = await client.complete(messages=[LLMMessage(role="user", content="hello")])

    assert response.usage.input_tokens == 0
    assert response.usage.output_tokens == 0
    assert recorder.records[0][1] == Decimal("0")


async def test_tool_call_without_function_is_skipped() -> None:
    completion = _completion(
        choices=[
            SimpleNamespace(
                finish_reason="tool_calls",
                message=SimpleNamespace(
                    content="",
                    tool_calls=[SimpleNamespace(id="call_1", type="function")],
                ),
            )
        ]
    )
    stub = StubDeepSeek(completion)
    client = _client(stub, RecordingRecorder())

    response = await client.complete(messages=[LLMMessage(role="user", content="hello")])

    assert response.tool_calls == ()


async def test_no_choices_raises_value_error() -> None:
    completion = _completion(choices=[])
    stub = StubDeepSeek(completion)
    client = _client(stub, RecordingRecorder())

    with pytest.raises(ValueError, match="no choices"):
        await client.complete(messages=[LLMMessage(role="user", content="hello")])


@pytest.mark.parametrize("kind", ["rate", "status", "connection"])
async def test_sdk_errors_are_reraises(kind: str) -> None:
    stub = StubDeepSeek(_completion(), error=_openai_error(kind))
    client = _client(stub, RecordingRecorder())

    with pytest.raises((openai.RateLimitError, openai.APIStatusError, openai.APIConnectionError)):
        await client.complete(messages=[LLMMessage(role="user", content="hello")])


async def test_reasoning_content_is_returned_for_the_tool_loop() -> None:
    message = SimpleNamespace(content="", tool_calls=None, reasoning_content="step by step")
    stub = StubDeepSeek(
        _completion(choices=[SimpleNamespace(finish_reason="stop", message=message)])
    )

    response = await _client(stub, RecordingRecorder()).complete(
        messages=[LLMMessage(role="user", content="hi")]
    )

    assert response.reasoning_content == "step by step"


async def test_reasoning_content_can_arrive_in_model_extra() -> None:
    message = SimpleNamespace(
        content="hi", tool_calls=None, model_extra={"reasoning_content": "extra"}
    )
    stub = StubDeepSeek(
        _completion(choices=[SimpleNamespace(finish_reason="stop", message=message)])
    )

    response = await _client(stub, RecordingRecorder()).complete(
        messages=[LLMMessage(role="user", content="hi")]
    )

    assert response.reasoning_content == "extra"


async def test_no_reasoning_content_is_none() -> None:
    response = await _client(StubDeepSeek(_completion()), RecordingRecorder()).complete(
        messages=[LLMMessage(role="user", content="hi")]
    )

    assert response.reasoning_content is None
