"""P6: the lead planning code runs unchanged on a DeepSeek-backed StructuredLLM."""

from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace
from typing import Any

from pydantic import SecretStr

from gaia.domain.plans import Plan, TicketInput
from gaia.llm.base import LLMUsage
from gaia.llm.openai_compat_client import DeepSeekClient
from gaia.orchestrator.lead import LeadEngineer
from gaia.orchestrator.repo_context import RepoContext
from gaia.security.commands import CommandPolicy
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
    def __init__(self, content: str) -> None:
        self._content = content
        self.create_calls: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> Any:
        self.create_calls.append(kwargs)
        return SimpleNamespace(
            model="deepseek-v4-pro",
            choices=[
                SimpleNamespace(
                    finish_reason="stop",
                    message=SimpleNamespace(content=self._content, tool_calls=None),
                )
            ],
            usage=SimpleNamespace(prompt_tokens=20, completion_tokens=5),
            _request_id="req_portability",
        )


class StubClient:
    def __init__(self, content: str) -> None:
        self.chat = SimpleNamespace(completions=StubCompletions(content))


def _price_table() -> PriceTable:
    return PriceTable(
        {"deepseek-v4-pro": ModelPrice(rates=PriceRates(input=Decimal("1"), output=Decimal("2")))}
    )


def _deepseek_structured_llm(content: str) -> tuple[DeepSeekClient, RecordingRecorder, StubClient]:
    stub = StubClient(content)
    recorder = RecordingRecorder()
    client = DeepSeekClient(
        api_key=SecretStr("sk-deepseek-test-1234567890"),
        model="deepseek-v4-pro",
        base_url="https://api.deepseek.com",
        recorder=recorder,
        price_table=_price_table(),
        client=stub,
    )
    return client, recorder, stub


def _ticket() -> TicketInput:
    return TicketInput(id="t1", title="Ship", body="Make the tests pass")


def _repo_context() -> RepoContext:
    return RepoContext(readme=None, tree="src/", files=(), omitted=())


PLAN_JSON = (
    '{"summary":"Add a test","risks":[],"steps":[{"title":"Write a test",'
    '"instructions":"Add a failing test and make it pass.","files_likely_touched":'
    '["tests/test_x.py"],"acceptance":["pytest passes"],"test_command":["pytest","-q"]}]}'
)

REVIEW_JSON = '{"verdict":"accept","feedback":"looks good","issues":[]}'


async def test_plan_ticket_runs_unchanged_with_deepseek_parse() -> None:
    client, recorder, stub = _deepseek_structured_llm(PLAN_JSON)
    engineer = LeadEngineer(client, CommandPolicy.from_default())

    plan = await engineer.plan_ticket(_ticket(), _repo_context())

    assert plan.steps[0].test_command == ["pytest", "-q"]
    assert len(stub.chat.completions.create_calls) == 1
    assert len(recorder.records) == 1
    request = stub.chat.completions.create_calls[0]
    assert request["response_format"] == {"type": "json_object"}


async def test_review_step_runs_unchanged_with_deepseek_parse() -> None:
    client, recorder, stub = _deepseek_structured_llm(REVIEW_JSON)
    engineer = LeadEngineer(client, CommandPolicy.from_default())

    plan = Plan.model_validate_json(PLAN_JSON)
    review = await engineer.review_step(plan, 0, "diff --git a/a.py b/a.py\n+x", "passed")

    assert review.verdict == "accept"
    assert len(stub.chat.completions.create_calls) == 1
    assert len(recorder.records) == 1
