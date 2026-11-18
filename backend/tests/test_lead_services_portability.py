"""P5/P6: the lead's planner and reviewer never assume Claude.

The portability test runs every real service method once per configured
provider, with a stub structured LLM. The second test is the one gateway-path
case (DeepSeek only): it drives the real ``DeepSeekClient`` through the app's
in-process gateway and asserts the gateway-owned usage/cost behaviour.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal, cast

import httpx
import httpx2
import pytest
from pydantic import SecretStr

from gaia.api import app as app_module
from gaia.api.app import create_app
from gaia.config.secrets import InMemorySecretStore, SecretName
from gaia.config.settings import Settings
from gaia.db.engine import session_scope
from gaia.db.models import LlmUsage as LlmUsageRow
from gaia.db.models import Run, RunStep, RunToken, Worktree
from gaia.db.repo import RunRepo, ScopedRepo
from gaia.domain.plans import Plan, PlanStep, Review
from gaia.llm.base import LLMUsage, ParsedResponse
from gaia.llm.openai_compat_client import DeepSeekClient
from gaia.orchestrator.deps import CheckOutcome, StepOutcome
from gaia.orchestrator.impl import lead_llm as lead_llm_module
from gaia.orchestrator.impl import planning as planning_module
from gaia.orchestrator.impl.planning import GatewayPlanner
from gaia.orchestrator.impl.review import GatewayReviewer
from gaia.orchestrator.repo_context import RepoContext
from gaia.services.costs import PriceTable, load_price_table
from gaia.services.git import GitService
from tests.db_builders import make_chain

PLAN_STEP = PlanStep(
    title="Write a test",
    instructions="Add a failing test and make it pass.",
    files_likely_touched=["tests/test_x.py"],
    acceptance=["pytest passes"],
    test_command=["uv", "run", "pytest", "-q"],
)
PLAN = Plan(summary="Add a test", risks=[], steps=[PLAN_STEP])
REVIEW = Review(verdict="accept", feedback="looks good", issues=[])
PLAN_JSON = PLAN.model_dump_json()
REVIEW_JSON = REVIEW.model_dump_json()

DEEPSEEK_KEY = "sk-deepseek-test-" + "x" * 40


class ChunkedBody(httpx.AsyncByteStream):
    """An async response body that yields fixed chunks."""

    def __init__(self, chunks: list[bytes]) -> None:
        self.chunks = chunks

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for chunk in self.chunks:
            yield chunk


class StubStructuredLLM:
    """A provider-neutral structured LLM that returns scripted Plan/Review."""

    def __init__(self) -> None:
        self.parse_calls: list[type[Any]] = []
        self.closed = False

    async def parse(
        self,
        *,
        system: str,
        messages: Any,
        output_model: type[Any],
        max_tokens: int = 16000,
        enable_fallback: bool = False,
    ) -> ParsedResponse[Any]:
        del system, messages, max_tokens, enable_fallback
        self.parse_calls.append(output_model)
        if output_model is Plan:
            output: Any = Plan.model_validate_json(PLAN_JSON)
        elif output_model is Review:
            output = Review.model_validate_json(REVIEW_JSON)
        else:
            raise AssertionError(f"unexpected output model {output_model!r}")
        return ParsedResponse(
            output=output,
            usage=LLMUsage(provider="stub", model="stub-model", input_tokens=1, output_tokens=1),
            model="stub-model",
            request_id=None,
            stop_reason="end_turn",
        )

    async def aclose(self) -> None:
        self.closed = True


class FakeGit:
    def __init__(self) -> None:
        self.diff_calls: list[tuple[Any, str, str]] = []

    async def diff(self, repo: Any, base: str, head: str) -> str:
        self.diff_calls.append((repo, base, head))
        return "diff --git a/a.py b/a.py\n+change\n"


async def _fake_repo_context(root: Path, ticket: Any, **kwargs: Any) -> RepoContext:
    del root, ticket, kwargs
    return RepoContext(readme=None, tree="src/", files=(), omitted=())


async def _prepare_chain(database: Any, slug: str) -> Any:
    async with session_scope(database.sessions) as session:
        chain = await make_chain(session, slug=slug)
        steps = ScopedRepo(session, RunStep, chain.workspace.id)
        await steps.update(chain.step.id, spec_json=PLAN_STEP.model_dump())
        worktrees = ScopedRepo(session, Worktree, chain.workspace.id)
        await worktrees.update(chain.worktree.id, git_dir="/tmp/gitdir")
    return chain


@pytest.mark.parametrize(
    ("provider", "model"),
    [("anthropic", "claude-opus-5-5"), ("deepseek", "deepseek-v4-pro")],
)
async def test_lead_services_run_on_each_provider(
    database: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    provider: Literal["anthropic", "deepseek"],
    model: str,
) -> None:
    chain = await _prepare_chain(database, f"portability-{provider}")

    factory_calls: list[dict[str, Any]] = []
    stubs: list[StubStructuredLLM] = []

    def fake_build_structured_llm(
        *,
        provider: str,
        model: str,
        api_key: SecretStr,
        base_url: str,
        recorder: Any,
        price_table: Any,
    ) -> StubStructuredLLM:
        del recorder, price_table
        factory_calls.append(
            {
                "provider": provider,
                "model": model,
                "api_key": api_key.get_secret_value(),
                "base_url": base_url,
            }
        )
        stub = StubStructuredLLM()
        stubs.append(stub)
        return stub

    monkeypatch.setattr(lead_llm_module, "build_structured_llm", fake_build_structured_llm)
    monkeypatch.setattr(planning_module, "build_repo_context", _fake_repo_context)

    settings = Settings(
        planner_provider=provider,
        planner_model=model,
        data_dir=tmp_path / "gaia-data",
    )
    fake_git = FakeGit()
    planner = GatewayPlanner(
        settings=settings,
        sessions=database.sessions,
        price_table=PriceTable({}),
    )
    reviewer = GatewayReviewer(
        settings=settings,
        sessions=database.sessions,
        price_table=PriceTable({}),
        git=cast(GitService, fake_git),
    )

    plan = await planner.plan(chain.run.id)
    step_review = await reviewer.review_step(
        chain.run.id,
        0,
        StepOutcome(status="completed", summary="done", files_changed=["a.py"], diff="diff"),
        CheckOutcome(passed=True, output="tests passed"),
    )
    final_review = await reviewer.final_review(chain.run.id)

    assert plan == PLAN
    assert step_review == REVIEW
    assert final_review == REVIEW
    assert len(factory_calls) == 3
    assert all(call["provider"] == provider for call in factory_calls)
    assert all(call["model"] == model for call in factory_calls)
    assert all(call["base_url"].endswith(f"/llm/{provider}") for call in factory_calls)
    assert all(call["api_key"].startswith("gaia_rt_") for call in factory_calls)
    assert all(stub.closed for stub in stubs)

    async with session_scope(database.sessions) as session:
        tokens = await ScopedRepo(session, RunToken, chain.workspace.id).list()
    assert len(tokens) == 3
    assert all(token.revoked_at is not None for token in tokens)


async def test_deepseek_gateway_path_records_usage_and_cost(
    database: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One real SDK client through the in-process gateway (DeepSeek only)."""
    store = InMemorySecretStore()
    store.set(SecretName.GAIA_API_TOKEN, "admin-token")
    store.set(SecretName.DEEPSEEK_API_KEY, DEEPSEEK_KEY)
    monkeypatch.setattr(app_module, "get_secret_store", lambda: store)

    app_settings = Settings(
        data_dir=tmp_path / "gaia-data",
        db_url=database.url,
        planner_provider="deepseek",
        planner_model="deepseek-v4-pro",
        anthropic_base_url="https://anthropic.test",
        junior_base_url="https://deepseek.test",
    )

    upstream_seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        upstream_seen["path"] = request.url.path
        upstream_seen["headers"] = dict(request.headers)
        upstream_seen["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            headers={"content-type": "application/json", "request-id": "req_deepseek_gateway"},
            stream=ChunkedBody(
                [
                    json.dumps(
                        {
                            "model": "deepseek-v4-pro",
                            "choices": [{"message": {"content": PLAN_JSON}}],
                            "usage": {"prompt_tokens": 40, "completion_tokens": 6},
                        }
                    ).encode()
                ]
            ),
        )

    app = create_app(
        app_settings,
        llm_transport=httpx.MockTransport(lambda request: handler(request)),
    )

    async with app.router.lifespan_context(app):
        chain = await _prepare_chain(database, "deepseek-gateway")
        async with session_scope(database.sessions) as session:
            runs = ScopedRepo(session, Run, chain.workspace.id)
            await runs.update(chain.run.id, budget_usd=Decimal("5.0"))

        monkeypatch.setattr(planning_module, "build_repo_context", _fake_repo_context)
        factory_calls: list[dict[str, Any]] = []

        def fake_build_structured_llm(
            *,
            provider: str,
            model: str,
            api_key: SecretStr,
            base_url: str,
            recorder: Any,
            price_table: Any,
        ) -> DeepSeekClient:
            factory_calls.append(
                {
                    "provider": provider,
                    "model": model,
                    "api_key": api_key.get_secret_value(),
                    "base_url": base_url,
                }
            )
            return DeepSeekClient(
                api_key=api_key,
                model=model,
                base_url=base_url,
                recorder=recorder,
                price_table=price_table,
                http_client=sdk_http_client,
            )

        monkeypatch.setattr(lead_llm_module, "build_structured_llm", fake_build_structured_llm)

        async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app)) as sdk_http_client:
            planner = GatewayPlanner(
                settings=app_settings,
                sessions=database.sessions,
                price_table=load_price_table(),
            )
            plan = await planner.plan(chain.run.id)

        assert plan == PLAN
        assert len(factory_calls) == 1
        assert factory_calls[0]["provider"] == "deepseek"
        assert factory_calls[0]["model"] == "deepseek-v4-pro"
        assert factory_calls[0]["base_url"].endswith("/llm/deepseek")
        assert factory_calls[0]["api_key"].startswith("gaia_rt_")
        assert factory_calls[0]["api_key"] != DEEPSEEK_KEY

        # The upstream saw only the gateway-injected real key, never the token.
        assert upstream_seen["path"] == "/chat/completions"
        assert upstream_seen["headers"]["authorization"] == f"Bearer {DEEPSEEK_KEY}"
        assert "gaia_rt_" not in repr(upstream_seen["headers"])
        assert upstream_seen["body"]["response_format"] == {"type": "json_object"}

        async with session_scope(database.sessions) as session:
            usage_rows = await ScopedRepo(session, LlmUsageRow, chain.workspace.id).list()
            assert len(usage_rows) == 1
            usage = usage_rows[0]
            assert usage.provider == "deepseek"
            assert usage.model == "deepseek-v4-pro"
            assert usage.input_tokens == 40
            assert usage.output_tokens == 6
            assert usage.cost_usd > 0

            run = await RunRepo(session).get_by_id(chain.run.id)
            assert run is not None
            assert run.cost_usd == usage.cost_usd
            assert run.cost_usd > 0

            tokens = await ScopedRepo(session, RunToken, chain.workspace.id).list()
        assert len(tokens) == 1
        assert tokens[0].revoked_at is not None
