"""The fake provider transport, tested through the real SDK clients.

The installed Anthropic and OpenAI SDKs use ``httpx2`` rather than ``httpx``.
The gateway uses the production :class:`FakeProviderTransport` directly as an
``httpx.AsyncBaseTransport``; these tests adapt the same transport into an
``httpx2.MockTransport`` so the **real** ``AnthropicClient`` and
``DeepSeekClient`` parse its bytes instead of a hand-written expectation.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import httpx
import httpx2
import pytest
from pydantic import SecretStr

from gaia.dev import fake_llm as fake_llm_module
from gaia.dev.fake_llm import DEMO_PLAN_JSON, DEMO_REVIEW_JSON, FakeProviderTransport
from gaia.domain.plans import Plan, Review
from gaia.llm.anthropic_client import AnthropicClient
from gaia.llm.base import LLMMessage, LLMTool
from gaia.llm.deepseek_client import DeepSeekClient
from gaia.services.costs import ModelPrice, PriceRates, PriceTable


class RecordingRecorder:
    """An in-memory :class:`~gaia.llm.base.UsageRecorder`."""

    def __init__(self) -> None:
        self.records: list[Any] = []

    async def record(
        self,
        usage: Any,
        *,
        cost_usd: Decimal,
        price_tier: str | None = None,
    ) -> None:
        self.records.append((usage, cost_usd, price_tier))


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
            ),
            "deepseek-v4-pro": ModelPrice(
                rates=PriceRates(
                    input=Decimal("1"),
                    output=Decimal("2"),
                    cache_read=Decimal("0.5"),
                )
            ),
        }
    )


def _httpx2_transport() -> httpx2.MockTransport:
    """Adapt the production httpx transport into an httpx2 MockTransport."""
    core = FakeProviderTransport()

    async def handler(request: httpx2.Request) -> httpx2.Response:
        httpx_request = httpx.Request(
            request.method,
            str(request.url),
            headers=request.headers,
            content=await request.aread(),
        )
        response = await core.handle_async_request(httpx_request)
        content = await response.aread()
        return httpx2.Response(
            response.status_code,
            headers=dict(response.headers),
            content=content,
        )

    return httpx2.MockTransport(handler)


def _anthropic_client(recorder: RecordingRecorder) -> tuple[AnthropicClient, httpx2.AsyncClient]:
    http_client = httpx2.AsyncClient(transport=_httpx2_transport())
    client = AnthropicClient(
        api_key=SecretStr("sk-ant-test-1234567890"),
        model="claude-opus-5-5",
        recorder=recorder,
        price_table=_price_table(),
        base_url="https://api.anthropic.com",
        http_client=http_client,
    )
    return client, http_client


def _deepseek_client(recorder: RecordingRecorder) -> tuple[DeepSeekClient, httpx2.AsyncClient]:
    http_client = httpx2.AsyncClient(transport=_httpx2_transport())
    client = DeepSeekClient(
        api_key=SecretStr("sk-deepseek-test-1234567890"),
        model="deepseek-v4-pro",
        base_url="https://api.deepseek.com",
        recorder=recorder,
        price_table=_price_table(),
        http_client=http_client,
    )
    return client, http_client


async def test_anthropic_parse_plan_uses_the_real_client() -> None:
    client, http_client = _anthropic_client(RecordingRecorder())
    try:
        response = await client.parse(
            system="plan",
            messages=[LLMMessage(role="user", content="Plan this ticket")],
            output_model=Plan,
        )
    finally:
        await client.aclose()
        await http_client.aclose()

    assert isinstance(response.output, Plan)
    assert response.output.steps[0].title == "Add a demo note"
    assert response.output.steps[0].test_command is None


async def test_anthropic_parse_review_uses_the_real_client() -> None:
    client, http_client = _anthropic_client(RecordingRecorder())
    try:
        response = await client.parse(
            system="review",
            messages=[LLMMessage(role="user", content="Review this diff")],
            output_model=Review,
        )
    finally:
        await client.aclose()
        await http_client.aclose()

    assert isinstance(response.output, Review)
    assert response.output.verdict == "accept"
    assert response.output.issues == []


async def test_anthropic_complete_non_streamed_uses_the_real_client() -> None:
    recorder = RecordingRecorder()
    client, http_client = _anthropic_client(recorder)
    try:
        response = await client.complete(
            system="sys",
            messages=[LLMMessage(role="user", content="hello")],
            max_tokens=100,
        )
    finally:
        await client.aclose()
        await http_client.aclose()

    assert response.text == "demo response"
    assert recorder.records
    assert recorder.records[0][0].output_tokens == 200


async def test_deepseek_parse_plan_uses_the_real_client() -> None:
    client, http_client = _deepseek_client(RecordingRecorder())
    try:
        response = await client.parse(
            system="plan",
            messages=[LLMMessage(role="user", content="Plan this ticket")],
            output_model=Plan,
        )
    finally:
        await client.aclose()
        await http_client.aclose()

    assert isinstance(response.output, Plan)
    assert response.output.steps[0].title == "Add a demo note"


async def test_deepseek_tool_loop_turns_use_the_real_client() -> None:
    client, http_client = _deepseek_client(RecordingRecorder())
    tools = (
        LLMTool(
            name="write_file",
            description="Write a file",
            input_schema={"type": "object", "properties": {}},
        ),
        LLMTool(
            name="run_command",
            description="Run a command",
            input_schema={"type": "object", "properties": {}},
        ),
    )
    messages: list[LLMMessage] = [
        LLMMessage(role="system", content="sys"),
        LLMMessage(
            role="user",
            content=(
                "Title: Add a demo note\n"
                "## Test command\n"
                '<untrusted_content source="plan:test-command">'
                "python3 -m unittest -q</untrusted_content>"
            ),
        ),
    ]
    try:
        first = await client.complete(messages=messages, tools=tools)
        assert [call.name for call in first.tool_calls] == ["write_file"]

        messages.append(
            LLMMessage(
                role="assistant",
                content=first.text,
                tool_calls=first.tool_calls,
                reasoning_content=first.reasoning_content,
            )
        )
        messages.append(LLMMessage(role="tool", tool_call_id="call_1", content="wrote file"))
        second = await client.complete(messages=messages, tools=tools)
        assert [call.name for call in second.tool_calls] == ["run_command"]
        assert second.tool_calls[0].arguments["argv"] == [
            "python3",
            "-m",
            "unittest",
            "-q",
        ]

        messages.append(
            LLMMessage(
                role="assistant",
                content=second.text,
                tool_calls=second.tool_calls,
                reasoning_content=second.reasoning_content,
            )
        )
        messages.append(
            LLMMessage(
                role="tool",
                tool_call_id="call_2",
                content="exit=0\n--- stdout ---\n",
            )
        )
        third = await client.complete(messages=messages, tools=tools)
        assert [call.name for call in third.tool_calls] == ["finish"]
        assert third.tool_calls[0].arguments["tests_passed"] is True
        assert third.tool_calls[0].arguments["tests_run"] == ["python3 -m unittest -q"]
    finally:
        await client.aclose()
        await http_client.aclose()


async def test_deepseek_tool_loop_skips_tests_when_none_are_named() -> None:
    client, http_client = _deepseek_client(RecordingRecorder())
    tools = (
        LLMTool(
            name="write_file",
            description="Write a file",
            input_schema={"type": "object", "properties": {}},
        ),
        LLMTool(
            name="run_command",
            description="Run a command",
            input_schema={"type": "object", "properties": {}},
        ),
    )
    messages: list[LLMMessage] = [
        LLMMessage(role="system", content="sys"),
        LLMMessage(role="user", content="Title: Add a demo note"),
    ]
    try:
        first = await client.complete(messages=messages, tools=tools)
        assert [call.name for call in first.tool_calls] == ["write_file"]

        messages.append(
            LLMMessage(
                role="assistant",
                content=first.text,
                tool_calls=first.tool_calls,
                reasoning_content=first.reasoning_content,
            )
        )
        messages.append(LLMMessage(role="tool", tool_call_id="call_1", content="wrote file"))
        second = await client.complete(messages=messages, tools=tools)
        assert [call.name for call in second.tool_calls] == ["finish"]
        assert second.tool_calls[0].arguments["tests_passed"] is False
        assert second.tool_calls[0].arguments["tests_run"] == []
    finally:
        await client.aclose()
        await http_client.aclose()


async def test_streamed_deepseek_bytes_are_metrically_parseable() -> None:
    async with httpx.AsyncClient(transport=FakeProviderTransport()) as client:
        response = await client.post(
            "https://deepseek.test/chat/completions",
            json={"model": "deepseek-v4-pro", "stream": True, "messages": []},
        )

    assert response.status_code == 200
    assert b"data: [DONE]" in response.content
    assert b'"prompt_tokens":800' in response.content
    assert b'"completion_tokens":200' in response.content


async def test_unknown_request_is_a_provider_style_400() -> None:
    async with httpx.AsyncClient(transport=FakeProviderTransport()) as client:
        response = await client.post(
            "https://anthropic.test/v1/unknown",
            json={"model": "claude-opus-5-5"},
        )

    assert response.status_code == 400
    body = response.json()
    assert body["type"] == "error"
    assert body["error"]["type"] == "invalid_request_error"


@pytest.mark.parametrize("raw", [b"not-json", b"[1, 2, 3]"])
async def test_invalid_bodies_are_provider_style_400(raw: bytes) -> None:
    async with httpx.AsyncClient(transport=FakeProviderTransport()) as client:
        response = await client.post(
            "https://deepseek.test/chat/completions",
            content=raw,
            headers={"content-type": "application/json"},
        )

    assert response.status_code == 400
    assert response.json()["error"]["type"] == "invalid_request_error"


async def test_anthropic_count_tokens_is_answered_without_metering() -> None:
    async with httpx.AsyncClient(transport=FakeProviderTransport()) as client:
        response = await client.post(
            "https://anthropic.test/v1/messages/count_tokens",
            json={"model": "claude-opus-5-5", "messages": []},
        )

    assert response.status_code == 200
    assert response.json() == {"input_tokens": 800}


def test_anthropic_text_covers_generic_output_format() -> None:
    transport = FakeProviderTransport()
    assert transport._anthropic_text({"output_format": {"type": "json_schema"}}) == (
        '{"message":"demo structured response"}'
    )


def test_schema_contains_reads_top_level_output_format() -> None:
    assert (
        fake_llm_module._schema_contains({"output_format": {"properties": {"steps": {}}}}, "steps")
        is True
    )


def test_deepseek_parse_review_and_generic_without_messages() -> None:
    transport = FakeProviderTransport()

    review = transport._deepseek_text(
        {
            "response_format": {"type": "json_object"},
            "messages": [{"role": "system", "content": 'schema with "verdict"'}],
        }
    )
    assert review == DEMO_REVIEW_JSON

    generic = transport._deepseek_text(
        {
            "response_format": {"type": "json_object"},
            "messages": "not-a-list",
        }
    )
    assert generic == '{"message":"demo structured response"}'


async def test_deepseek_tool_response_without_messages_is_still_valid() -> None:
    transport = FakeProviderTransport()
    response = transport._deepseek_tool_response({"tools": []}, "deepseek-v4-pro")

    assert response.status_code == 200
    assert b"demo response" in await response.aread()


def test_title_falls_back_when_no_title_line_exists() -> None:
    assert fake_llm_module._title_from_instructions("just instructions") == "just instructions"
    assert fake_llm_module._title_from_instructions("") == "demo ticket"


def test_test_command_fallback_and_invalid_shlex() -> None:
    assert fake_llm_module._test_command_from_instructions(
        "Test command: python3 -m unittest -q"
    ) == ["python3", "-m", "unittest", "-q"]
    assert fake_llm_module._test_command_from_instructions("Test command: 'unclosed") is None


def test_latest_command_passed_without_tool_messages() -> None:
    assert fake_llm_module._latest_command_passed([]) is False


def test_plan_and_review_json_are_valid_for_the_real_models() -> None:
    assert Plan.model_validate_json(DEMO_PLAN_JSON).steps[0].title == "Add a demo note"
    assert Review.model_validate_json(DEMO_REVIEW_JSON).verdict == "accept"


def test_schema_contains_falls_back_to_output_config_when_schema_is_not_a_dict() -> None:
    payload = {"output_config": {"format": {"schema": "not-a-dict", "steps": {}}}}
    assert fake_llm_module._schema_contains(payload, "steps") is True


def test_system_prompt_ignores_non_dict_messages() -> None:
    assert fake_llm_module._system_prompt({"messages": [42]}) == ""


def test_first_user_message_falls_back_to_empty_string() -> None:
    assert fake_llm_module._first_user_message([{"role": "system", "content": "sys"}]) == ""


def test_title_with_empty_title_line_falls_back() -> None:
    assert fake_llm_module._title_from_instructions("Title:\nfallback") == "fallback"


def test_latest_command_passed_ignores_non_tool_messages() -> None:
    assert fake_llm_module._latest_command_passed([{"role": "user", "content": "hi"}]) is False
