"""Value objects and exceptions shared by the LLM clients (doc 07)."""

from __future__ import annotations

from decimal import Decimal

from gaia.llm.base import LLMMessage, LLMResponse, LLMStop, LLMUsage, ToolCall


def test_llm_stop_message_without_category() -> None:
    error = LLMStop("max_tokens", request_id="req_1")
    assert str(error) == "LLM call stopped: max_tokens"
    assert error.stop_reason == "max_tokens"
    assert error.refusal_category is None
    assert error.request_id == "req_1"


def test_llm_stop_message_with_refusal_category() -> None:
    error = LLMStop("refusal", refusal_category="cyber", request_id="req_2")
    assert str(error) == "LLM call stopped: refusal (cyber)"
    assert error.refusal_category == "cyber"


def test_message_and_response_shapes() -> None:
    tool_call = ToolCall(id="tool_1", name="read", arguments={"path": "x"})
    message = LLMMessage(
        role="assistant",
        content="",
        tool_calls=(tool_call,),
        reasoning_content="thinking",
    )
    usage = LLMUsage(
        provider="fake",
        model="fake",
        input_tokens=1,
        output_tokens=2,
        cache_read_tokens=3,
        cache_creation_tokens=4,
        request_id="req_3",
    )
    response = LLMResponse(text="hello", usage=usage, tool_calls=(tool_call,))
    assert message.reasoning_content == "thinking"
    assert response.text == "hello"
    assert response.usage.cache_creation_tokens == 4
    assert isinstance(response.usage.cache_creation_tokens, int)
    assert isinstance(usage.input_tokens, int)
    assert Decimal(usage.output_tokens) == Decimal(2)
