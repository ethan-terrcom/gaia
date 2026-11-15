"""ADR-001 P5: the full ticket run with no Claude anywhere.

The test builds the production composition root with both the junior and lead
roles on the native DeepSeek runtime, drives a real ticket from ``pending`` to
``completed`` through the real graph, and points every LLM client at the
in-process gateway. The upstream provider is a scripted DeepSeek fake; the
Claude Agent SDK runtime is patched so the test fails if anything calls it.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import httpx2
import pytest

from gaia.api import app as app_module
from gaia.api.app import create_app
from gaia.config.secrets import InMemorySecretStore, SecretName
from gaia.config.settings import Settings
from gaia.db.engine import session_scope
from gaia.db.models import LlmUsage as LlmUsageRow
from gaia.db.models import Repository, Run, RunToken, Ticket, User, Worktree
from gaia.db.repo import ScopedRepo
from gaia.domain.enums import RunStatus, RuntimeKind, TicketStatus
from gaia.domain.plans import Plan, PlanStep, Review
from gaia.orchestrator.graph import build_graph
from gaia.orchestrator.impl import worktree_location
from gaia.orchestrator.runner import RunManager, open_checkpointer
from gaia.orchestrator.wiring import build_wiring
from gaia.runtimes.claude_agent_sdk import ClaudeAgentSdkRuntime
from gaia.services.git import GitService
from tests.db_builders import make_workspace
from tests.orchestrator_fakes import FakePullRequests
from tests.tool_fakes import FakeCommandRunner

DEEPSEEK_KEY = "sk-deepseek-test-" + "x" * 40

PLAN_STEP = PlanStep(
    title="Add hello.py and run the tests",
    instructions="Write hello.py and then run pytest -q.",
    files_likely_touched=["hello.py"],
    acceptance=["pytest passes"],
    test_command=["pytest", "-q"],
)
PLAN = Plan(summary="Add hello.py", risks=[], steps=[PLAN_STEP])
REVIEW = Review(verdict="accept", feedback="looks good", issues=[])
PLAN_JSON = PLAN.model_dump_json()
REVIEW_JSON = REVIEW.model_dump_json()


class ChunkedBody(httpx.AsyncByteStream):
    """An async response body that yields fixed chunks."""

    def __init__(self, chunks: list[bytes]) -> None:
        self.chunks = chunks

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for chunk in self.chunks:
            yield chunk


class _ReusableAsyncClient(httpx2.AsyncClient):
    """An httpx2 client whose SDK close is a no-op until the test is done.

    The OpenAI SDK's ``AsyncOpenAI.close()`` closes the supplied http client
    after each per-call lead client is closed. The swap drill passes one shared
    ASGI client to several lead/native SDK clients, so the shared client must
    stay open until the test closes it explicitly.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._really_close = False

    async def aclose(self) -> None:
        if self._really_close:
            await super().aclose()

    async def close_for_real(self) -> None:
        self._really_close = True
        await self.aclose()


def _json_response(payload: dict[str, Any]) -> httpx.Response:
    return httpx.Response(
        200,
        headers={"content-type": "application/json", "request-id": "req_swap"},
        stream=ChunkedBody([json.dumps(payload).encode("utf-8")]),
    )


def _usage() -> dict[str, int]:
    return {
        "prompt_tokens": 20,
        "completion_tokens": 6,
        "prompt_cache_hit_tokens": 10,
        "prompt_cache_miss_tokens": 10,
    }


def _tool_call(call_id: str, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(arguments)},
    }


def _tool_response(model: str, tool_calls: list[dict[str, Any]]) -> httpx.Response:
    return _json_response(
        {
            "model": model,
            "choices": [
                {
                    "message": {"role": "assistant", "content": None, "tool_calls": tool_calls},
                    "finish_reason": "tool_calls",
                }
            ],
            "usage": _usage(),
        }
    )


def _make_deepseek_fake() -> tuple[
    Any,
    list[str],
    list[str],
]:
    seen_paths: list[str] = []
    seen_models: list[str] = []
    native_calls = 0

    def fake_deepseek(request: httpx.Request) -> httpx.Response:
        nonlocal native_calls
        payload = json.loads(request.content)
        seen_paths.append(request.url.path)
        model = payload.get("model")
        seen_models.append(model if isinstance(model, str) else "")

        if payload.get("response_format") == {"type": "json_object"}:
            system = payload["messages"][0]["content"]
            content = REVIEW_JSON if "verdict" in system else PLAN_JSON
            return _json_response(
                {
                    "model": model,
                    "choices": [
                        {
                            "message": {"role": "assistant", "content": content},
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": _usage(),
                }
            )

        if payload.get("tools"):
            native_calls += 1
            if native_calls == 1:
                tool_calls = [
                    _tool_call(
                        "call_1",
                        "write_file",
                        {"path": "hello.py", "content": "print('hello')\n"},
                    )
                ]
            elif native_calls == 2:
                tool_calls = [_tool_call("call_2", "run_command", {"argv": ["pytest", "-q"]})]
            else:
                tool_calls = [
                    _tool_call(
                        "call_3",
                        "finish",
                        {
                            "summary": "wrote hello.py and the tests passed",
                            "tests_run": ["pytest -q"],
                            "tests_passed": True,
                        },
                    )
                ]
            return _tool_response(str(model), tool_calls)

        raise AssertionError(f"unexpected DeepSeek request shape: {payload!r}")

    return fake_deepseek, seen_paths, seen_models


async def _wait_for_task(manager: RunManager, run_id: str, *, timeout_s: float = 60.0) -> None:
    """Wait for the current graph task for ``run_id`` to finish."""
    async with asyncio.timeout(timeout_s):
        await manager._tasks[run_id]


async def test_swap_drill_runs_a_ticket_end_to_end_without_claude(
    database: Any,
    git_repo: Path,
    git_settings: Settings,
    git_service: GitService,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = InMemorySecretStore()
    store.set(SecretName.GAIA_API_TOKEN, "admin-token")
    store.set(SecretName.DEEPSEEK_API_KEY, DEEPSEEK_KEY)
    monkeypatch.setattr(app_module, "get_secret_store", lambda: store)

    settings = Settings(
        data_dir=git_settings.data_dir,
        db_url=database.url,
        planner_provider="deepseek",
        planner_model="deepseek-v4-pro",
        junior_runtime=RuntimeKind.NATIVE_DEEPSEEK,
        lead_runtime=RuntimeKind.NATIVE_DEEPSEEK,
        orchestrator_enabled=False,
        pr_sync_enabled=False,
        run_budget_usd=5.0,
        junior_base_url="https://deepseek.test",
        anthropic_base_url="https://anthropic.test",
    )

    fake_deepseek, seen_paths, seen_models = _make_deepseek_fake()
    app = create_app(
        settings,
        llm_transport=httpx.MockTransport(lambda request: fake_deepseek(request)),
    )

    claude_called = False

    async def _fail_claude_run_task(task: Any, events: Any) -> Any:
        nonlocal claude_called
        del task, events
        claude_called = True
        raise AssertionError("ClaudeAgentSdkRuntime.run_task was called during the swap drill")

    monkeypatch.setattr(ClaudeAgentSdkRuntime, "run_task", _fail_claude_run_task)

    async with app.router.lifespan_context(app):
        services = app.state.services
        sessions = services.sessions
        bus = services.events
        git = services.git
        prices = services.prices
        secrets = services.store

        async with session_scope(sessions) as session:
            workspace = await make_workspace(session, slug="swap-drill")
            workspace_id = workspace.id
            users = ScopedRepo(session, User, workspace_id)
            user = await users.create(email="owner@swap.localhost", display_name="Owner")
            repositories = ScopedRepo(session, Repository, workspace_id)
            repository = await repositories.create(
                name="swap-drill",
                local_path=str(git_repo),
                setup_command=None,
                test_command=["pytest", "-q"],
                created_by=user.id,
            )
            tickets = ScopedRepo(session, Ticket, workspace_id)
            ticket = await tickets.create(
                repo_id=repository.id,
                title="Ship the swap drill",
                body="A body",
                created_by=user.id,
            )

        llm_http_client = _ReusableAsyncClient(transport=httpx2.ASGITransport(app=app))
        try:
            wiring = build_wiring(
                settings=settings,
                sessions=sessions,
                bus=bus,
                git=git,
                secrets=secrets,
                prices=prices,
                llm_http_client=llm_http_client,
                command_runner=FakeCommandRunner(),
                pull_requests=FakePullRequests(pr_number=1, default_ci="passed"),
            )

            async with open_checkpointer(settings) as saver:
                graph = build_graph(wiring.deps, checkpointer=saver)
                manager = RunManager(graph, wiring.deps)
                try:
                    run_id = await manager.start(
                        ticket.id,
                        workspace_id=workspace_id,
                        created_by=user.id,
                    )
                    await _wait_for_task(manager, run_id)

                    # Approve the plan.
                    await manager.resume(run_id, {"action": "approve"})
                    await _wait_for_task(manager, run_id)

                    # Approve opening the PR.
                    await manager.resume(run_id, {"action": "approve"})
                    await _wait_for_task(manager, run_id)

                    # Approve the merge.
                    await manager.resume(run_id, {"action": "approve"})
                    await _wait_for_task(manager, run_id)
                finally:
                    await manager.aclose()
        finally:
            await llm_http_client.close_for_real()

        async with session_scope(sessions) as session:
            run = await ScopedRepo(session, Run, workspace_id).get(run_id)
            assert run is not None
            assert run.status == RunStatus.COMPLETED.value
            assert run.cost_usd > 0
            assert run.worktree_id is not None

            ticket_row = await ScopedRepo(session, Ticket, workspace_id).get(ticket.id)
            assert ticket_row is not None
            assert ticket_row.status == TicketStatus.DONE.value

            usage_rows = await ScopedRepo(session, LlmUsageRow, workspace_id).list()
            assert usage_rows
            assert all(row.provider == "deepseek" for row in usage_rows)

            token_rows = await ScopedRepo(session, RunToken, workspace_id).list()
            assert token_rows
            assert all(token.revoked_at is not None for token in token_rows)

            worktree_row = await ScopedRepo(session, Worktree, workspace_id).get(run.worktree_id)
            assert worktree_row is not None

        assert worktree_row.base_sha is not None
        repo_path = await git_service.validate_repo(repository.local_path)
        location = await worktree_location(
            git=git_service,
            repo_path=repo_path,
            worktree=worktree_row,
        )
        changed_paths = await git_service.changed_paths(location, worktree_row.base_sha)
        assert "hello.py" in changed_paths

    assert seen_paths
    assert set(seen_paths) == {"/chat/completions"}
    assert all(model == "deepseek-v4-pro" for model in seen_models)
    assert not claude_called
