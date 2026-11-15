"""DeepSeekClient.parse: JSON mode, one repair, and provider-neutral P6."""

from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import httpx2
import openai
import pytest
from pydantic import BaseModel, SecretStr

from gaia.llm.base import LLMBudgetExceeded, LLMMessage, LLMStop, LLMUsage
from gaia.llm.deepseek_client import DeepSeekClient
from gaia.services.costs import ModelPrice, PriceRates, PriceTable


class Output(BaseModel):
    value: int


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
    def __init__(
        self,
        completions: list[Any] | None = None,
        errors: list[Exception] | None = None,
    ) -> None:
        self._completions = list(completions or [])
        self._errors = list(errors or [])
        self.create_calls: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> Any:
        self.create_calls.append(kwargs)
        if self._errors:
            raise self._errors.pop(0)
        if not self._completions:
            raise AssertionError("no completion scripted")
        return self._completions.pop(0)


class StubClient:
    def __init__(
        self,
        completions: list[Any] | None = None,
        errors: list[Exception] | None = None,
    ) -> None:
        self.chat = SimpleNamespace(completions=StubCompletions(completions, errors))


def _price_table() -> PriceTable:
    return PriceTable(
        {"deepseek-v4-pro": ModelPrice(rates=PriceRates(input=Decimal("1"), output=Decimal("2")))}
    )


def _client(stub: StubClient, recorder: RecordingRecorder) -> DeepSeekClient:
    return DeepSeekClient(
        api_key=SecretStr("sk-deepseek-test-1234567890"),
        model="deepseek-v4-pro",
        base_url="https://api.deepseek.com",
        recorder=recorder,
        price_table=_price_table(),
        client=stub,
    )


def _completion(
    content: str,
    *,
    finish_reason: str = "stop",
    prompt_tokens: int = 10,
    completion_tokens: int = 3,
) -> Any:
    return SimpleNamespace(
        model="deepseek-v4-pro",
        choices=[
            SimpleNamespace(
                finish_reason=finish_reason,
                message=SimpleNamespace(content=content, tool_calls=None),
            )
        ],
        usage=SimpleNamespace(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
        ),
        _request_id="req_parse",
    )


def _messages() -> list[LLMMessage]:
    return [LLMMessage(role="user", content="give me output")]


def _openai_error(kind: str) -> Exception:
    request = httpx2.Request("POST", "https://api.deepseek.com/chat/completions")
    if kind == "budget":
        return openai.PermissionDeniedError(
            "forbidden",
            response=httpx2.Response(
                403, request=request, headers={"x-request-id": "req_parse_budget"}
            ),
            body={"error": {"code": "budget_exceeded", "message": "x"}},
        )
    if kind == "permission":
        return openai.PermissionDeniedError(
            "forbidden",
            response=httpx2.Response(
                403, request=request, headers={"x-request-id": "req_parse_permission"}
            ),
            body={"error": {"code": "other"}},
        )
    if kind == "status":
        return openai.APIStatusError(
            "bad request",
            response=httpx2.Response(
                400, request=request, headers={"x-request-id": "req_parse_status"}
            ),
            body=None,
        )
    return openai.APIConnectionError(message="down", request=request)


async def test_valid_json_parses_first_time() -> None:
    recorder = RecordingRecorder()
    stub = StubClient([_completion('{"value": 7}')])
    client = _client(stub, recorder)

    response = await client.parse(
        system="prefix",
        messages=_messages(),
        output_model=Output,
    )

    assert response.output == Output(value=7)
    assert len(stub.chat.completions.create_calls) == 1
    assert len(recorder.records) == 1
    request = stub.chat.completions.create_calls[0]
    assert request["response_format"] == {"type": "json_object"}
    system_text = request["messages"][0]["content"]
    assert system_text.startswith("prefix\n\n")
    assert "JSON schema" in system_text
    assert '"value"' in system_text


async def test_blank_content_is_repaired_once() -> None:
    recorder = RecordingRecorder()
    stub = StubClient([_completion("   "), _completion('{"value": 8}')])
    client = _client(stub, recorder)

    response = await client.parse(
        system="prefix",
        messages=_messages(),
        output_model=Output,
    )

    assert response.output == Output(value=8)
    assert len(stub.chat.completions.create_calls) == 2
    assert len(recorder.records) == 2
    repair_request = stub.chat.completions.create_calls[1]
    assert repair_request["response_format"] == {"type": "json_object"}
    assert repair_request["messages"][-2]["role"] == "assistant"
    assert "Validation error:" in repair_request["messages"][-1]["content"]


async def test_invalid_json_twice_raises_invalid_output() -> None:
    recorder = RecordingRecorder()
    stub = StubClient([_completion("not-json"), _completion("still-not-json")])
    client = _client(stub, recorder)

    with pytest.raises(LLMStop) as caught:
        await client.parse(system="prefix", messages=_messages(), output_model=Output)

    assert caught.value.stop_reason == "invalid_output"
    assert len(recorder.records) == 2


async def test_length_finish_reason_maps_to_max_tokens() -> None:
    recorder = RecordingRecorder()
    stub = StubClient([_completion("", finish_reason="length")])
    client = _client(stub, recorder)

    with pytest.raises(LLMStop) as caught:
        await client.parse(system="prefix", messages=_messages(), output_model=Output)

    assert caught.value.stop_reason == "max_tokens"
    assert len(recorder.records) == 1


async def test_parse_content_filter_raises_refusal_after_recording() -> None:
    recorder = RecordingRecorder()
    stub = StubClient([_completion("", finish_reason="content_filter")])
    client = _client(stub, recorder)

    with pytest.raises(LLMStop) as caught:
        await client.parse(system="prefix", messages=_messages(), output_model=Output)

    assert caught.value.stop_reason == "refusal"
    assert len(recorder.records) == 1


async def test_parse_budget_refusal_translates_to_llm_budget_exceeded() -> None:
    recorder = RecordingRecorder()
    stub = StubClient(errors=[_openai_error("budget")])
    client = _client(stub, recorder)

    with pytest.raises(LLMBudgetExceeded):
        await client.parse(system="prefix", messages=_messages(), output_model=Output)


async def test_parse_permission_denied_non_budget_is_reraised() -> None:
    stub = StubClient(errors=[_openai_error("permission")])
    client = _client(stub, RecordingRecorder())

    with pytest.raises(openai.PermissionDeniedError):
        await client.parse(system="prefix", messages=_messages(), output_model=Output)


async def test_parse_api_status_error_is_reraised() -> None:
    stub = StubClient(errors=[_openai_error("status")])
    client = _client(stub, RecordingRecorder())

    with pytest.raises(openai.APIStatusError):
        await client.parse(system="prefix", messages=_messages(), output_model=Output)


async def test_parse_connection_error_is_reraised() -> None:
    stub = StubClient(errors=[_openai_error("connection")])
    client = _client(stub, RecordingRecorder())

    with pytest.raises(openai.APIConnectionError):
        await client.parse(system="prefix", messages=_messages(), output_model=Output)


async def test_schema_instruction_is_after_the_caller_system() -> None:
    recorder = RecordingRecorder()
    stub = StubClient([_completion('{"value": 1}')])
    client = _client(stub, recorder)

    await client.parse(system="system-prefix", messages=_messages(), output_model=Output)

    system_text = stub.chat.completions.create_calls[0]["messages"][0]["content"]
    instruction = system_text.split("system-prefix", 1)[1]
    assert instruction.startswith("\n\nRespond with a single JSON object")
    assert '"properties"' in system_text
