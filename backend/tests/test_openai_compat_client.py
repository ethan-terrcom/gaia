"""OpenAI-compatible client dialects for OpenAI and xAI (19a)."""

from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import BaseModel, SecretStr

from gaia.llm.base import LLMMessage, LLMUsage, ToolCall, ToolChatLLM
from gaia.llm.openai_compat_client import DeepSeekClient, OpenAICompatClient
from gaia.llm.providers import build_chat_llm, build_structured_llm
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
    def __init__(self, completions: list[Any]) -> None:
        self._completions = completions
        self.create_calls: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> Any:
        self.create_calls.append(kwargs)
        return self._completions.pop(0)


class StubOpenAI:
    def __init__(self, *completions: Any) -> None:
        self.chat = SimpleNamespace(completions=StubCompletions(list(completions)))


class Answer(BaseModel):
    answer: str


def _price_table() -> PriceTable:
    rates = PriceRates(input=Decimal("2"), output=Decimal("10"), cache_read=Decimal("0.1"))
    return PriceTable(
        {
            "gpt-6.2-sol": ModelPrice(rates=rates, provider="openai"),
            "grok-4.7": ModelPrice(rates=rates, provider="xai"),
            "deepseek-v4-pro": ModelPrice(rates=rates, provider="deepseek"),
        }
    )


def _completion(content: str = "hi", *, usage: Any = None, model: str = "gpt-6.2-sol") -> Any:
    return SimpleNamespace(
        model=model,
        choices=[
            SimpleNamespace(
                finish_reason="stop",
                message=SimpleNamespace(content=content, tool_calls=None),
            )
        ],
        usage=usage
        if usage is not None
        else SimpleNamespace(
            prompt_tokens=1000,
            completion_tokens=100,
            total_tokens=1100,
            prompt_tokens_details=SimpleNamespace(cached_tokens=400),
        ),
        _request_id="req_compat",
    )


def _client(
    provider: str, stub: StubOpenAI, recorder: RecordingRecorder, model: str = "gpt-6.2-sol"
) -> OpenAICompatClient:
    return OpenAICompatClient(
        provider=provider,
        api_key=SecretStr("gaia_rt_test"),
        model=model,
        base_url=f"http://127.0.0.1:8765/llm/{provider}",
        recorder=recorder,
        price_table=_price_table(),
        client=stub,
    )


async def test_openai_uses_max_completion_tokens_and_drops_reasoning_passback() -> None:
    stub = StubOpenAI(_completion())
    recorder = RecordingRecorder()
    client = _client("openai", stub, recorder)

    await client.complete(
        messages=[
            LLMMessage(role="user", content="hello"),
            LLMMessage(
                role="assistant",
                content="",
                reasoning_content="deepseek-only",
                tool_calls=(ToolCall(id="c1", name="read", arguments={}),),
            ),
        ],
        max_tokens=256,
    )

    request = stub.chat.completions.create_calls[0]
    assert request["max_completion_tokens"] == 256
    assert "max_tokens" not in request
    assert "reasoning_content" not in request["messages"][1]
    usage, _, _ = recorder.records[0]
    assert usage.provider == "openai"
    assert (usage.input_tokens, usage.cache_read_tokens, usage.output_tokens) == (600, 400, 100)
    assert client.provider == "openai"
    assert isinstance(client, ToolChatLLM)


async def test_xai_reasoning_outside_completion_tokens_is_billed_as_output() -> None:
    usage = SimpleNamespace(
        prompt_tokens=32,
        completion_tokens=9,
        total_tokens=135,
        model_extra={"prompt_tokens_details": {"cached_tokens": 6}},
    )
    stub = StubOpenAI(_completion(usage=usage, model="grok-4.7"))
    recorder = RecordingRecorder()
    client = _client("xai", stub, recorder, model="grok-4.7")

    await client.complete(messages=[LLMMessage(role="user", content="hi")])

    recorded, _, _ = recorder.records[0]
    assert recorded.provider == "xai"
    assert (recorded.input_tokens, recorded.cache_read_tokens, recorded.output_tokens) == (
        26,
        6,
        103,
    )


async def test_cached_tokens_can_arrive_as_a_plain_dict() -> None:
    usage = SimpleNamespace(
        prompt_tokens=10,
        completion_tokens=1,
        total_tokens=11,
        prompt_tokens_details={"cached_tokens": 4},
    )
    stub = StubOpenAI(_completion(usage=usage))
    recorder = RecordingRecorder()

    await _client("openai", stub, recorder).complete(
        messages=[LLMMessage(role="user", content="x")]
    )

    recorded, _, _ = recorder.records[0]
    assert (recorded.input_tokens, recorded.cache_read_tokens) == (6, 4)


async def test_missing_cached_detail_counts_every_prompt_token_as_input() -> None:
    usage = SimpleNamespace(
        prompt_tokens=10, completion_tokens=1, prompt_tokens_details={"cached_tokens": None}
    )
    stub = StubOpenAI(_completion(usage=usage))
    recorder = RecordingRecorder()

    await _client("openai", stub, recorder).complete(
        messages=[LLMMessage(role="user", content="x")]
    )

    recorded, _, _ = recorder.records[0]
    assert (recorded.input_tokens, recorded.cache_read_tokens, recorded.output_tokens) == (10, 0, 1)


async def test_parse_uses_the_providers_token_cap_and_json_mode() -> None:
    stub = StubOpenAI(_completion('{"answer": "42"}'))
    client = _client("xai", stub, RecordingRecorder(), model="grok-4.7")

    parsed = await client.parse(
        system="Be brief.", messages=[LLMMessage(role="user", content="q")], output_model=Answer
    )

    request = stub.chat.completions.create_calls[0]
    assert parsed.output == Answer(answer="42")
    assert request["response_format"] == {"type": "json_object"}
    assert "max_completion_tokens" in request
    assert "JSON" in request["messages"][0]["content"]


async def test_deepseek_keeps_max_tokens_and_reasoning_passback() -> None:
    stub = StubOpenAI(_completion(model="deepseek-v4-pro"))
    client = DeepSeekClient(
        api_key=SecretStr("gaia_rt_test"),
        model="deepseek-v4-pro",
        base_url="http://127.0.0.1:8765/llm/deepseek",
        recorder=RecordingRecorder(),
        price_table=_price_table(),
        client=stub,
    )

    await client.complete(
        messages=[LLMMessage(role="assistant", content="", reasoning_content="kept")],
        max_tokens=64,
    )

    request = stub.chat.completions.create_calls[0]
    assert request["max_tokens"] == 64
    assert request["messages"][0]["reasoning_content"] == "kept"
    assert client.provider == "deepseek"


def test_anthropic_is_refused_by_the_openai_compatible_client() -> None:
    with pytest.raises(ValueError, match="AnthropicClient"):
        _client("anthropic", StubOpenAI(), RecordingRecorder())


def test_an_unknown_provider_is_refused() -> None:
    with pytest.raises(ValueError, match="gemini"):
        _client("gemini", StubOpenAI(), RecordingRecorder())


@pytest.mark.parametrize(
    ("provider", "model", "expected"),
    [
        ("openai", "gpt-6.2-sol", OpenAICompatClient),
        ("xai", "grok-4.7", OpenAICompatClient),
        ("deepseek", "deepseek-v4-pro", DeepSeekClient),
    ],
)
def test_factories_pick_the_openai_compatible_adapter(
    provider: str, model: str, expected: type[OpenAICompatClient]
) -> None:
    common: dict[str, Any] = {
        "provider": provider,
        "model": model,
        "api_key": SecretStr("gaia_rt_test"),
        "base_url": f"http://127.0.0.1:8765/llm/{provider}",
        "recorder": RecordingRecorder(),
        "price_table": _price_table(),
    }

    chat = build_chat_llm(**common)
    structured = build_structured_llm(**common)

    assert type(chat) is expected
    assert type(structured) is expected
    assert chat.provider == provider
