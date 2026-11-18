"""Native LLM factory: one token per task, one gateway usage row per turn."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from gaia.api import app as app_module
from gaia.api.app import create_app
from gaia.config.secrets import InMemorySecretStore, SecretName
from gaia.config.settings import Settings
from gaia.db.engine import session_scope
from gaia.db.models import LlmUsage as LlmUsageRow
from gaia.db.models import Run, RunToken
from gaia.db.repo import ScopedRepo
from gaia.domain.enums import AgentRole
from gaia.llm.base import LLMMessage
from gaia.runtimes.base import AgentTask
from gaia.runtimes.native_llm import gateway_llm_factory
from gaia.services.git import WorktreeLocation
from tests.conftest import Database
from tests.db_builders import make_chain

DEEPSEEK_KEY = "sk-deepseek-test-" + "x" * 40


class ChunkedBody(httpx.AsyncByteStream):
    """An async response body that yields fixed chunks."""

    def __init__(self, chunks: list[bytes]) -> None:
        self.chunks = chunks

    async def __aiter__(self) -> Any:
        for chunk in self.chunks:
            yield chunk


def _unexpected(request: httpx.Request) -> httpx.Response:
    raise AssertionError(f"unexpected upstream request: {request.method} {request.url}")


def _json_response(payload: dict[str, Any]) -> httpx.Response:
    return httpx.Response(
        200,
        headers={"content-type": "application/json", "request-id": "req_native"},
        stream=ChunkedBody([json.dumps(payload).encode("utf-8")]),
    )


@dataclass
class NativeGateway:
    app: FastAPI
    settings: Settings
    store: InMemorySecretStore
    handlers: list[Callable[[httpx.Request], httpx.Response]]
    workspace_id: str
    run_id: str
    run_step_id: str
    agent_id: str


@asynccontextmanager
async def _started_app(
    tmp_path: Path,
    database: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[NativeGateway]:
    store = InMemorySecretStore()
    store.set(SecretName.GAIA_API_TOKEN, "admin-token")
    store.set(SecretName.DEEPSEEK_API_KEY, DEEPSEEK_KEY)
    monkeypatch.setattr(app_module, "get_secret_store", lambda: store)

    settings = Settings(
        data_dir=tmp_path / "gaia-data",
        db_url=database.url,
        junior_base_url="https://deepseek.test",
    )
    handlers: list[Callable[[httpx.Request], httpx.Response]] = [_unexpected]
    app = create_app(
        settings, llm_transport=httpx.MockTransport(lambda request: handlers[0](request))
    )

    async with app.router.lifespan_context(app):
        async with session_scope(database.sessions) as session:
            chain = await make_chain(session, slug="native-llm")
            runs = ScopedRepo(session, Run, chain.workspace.id)
            await runs.update(chain.run.id, budget_usd=Decimal("5.0"))
        yield NativeGateway(
            app=app,
            settings=settings,
            store=store,
            handlers=handlers,
            workspace_id=chain.workspace.id,
            run_id=chain.run.id,
            run_step_id=chain.step.id,
            agent_id=chain.agent.id,
        )


def _task(gateway: NativeGateway, tmp_path: Path) -> AgentTask:
    return AgentTask(
        task_id="task-native-llm",
        run_id=gateway.run_id,
        run_step_id=gateway.run_step_id,
        workspace_id=gateway.workspace_id,
        agent_id=gateway.agent_id,
        role=AgentRole.JUNIOR,
        instructions="say hi",
        worktree=WorktreeLocation(work_tree=tmp_path, git_dir=tmp_path / ".git"),
        base_sha="0" * 40,
    )


async def test_factory_turn_goes_through_gateway_and_records_exactly_one_row(
    tmp_path: Path,
    database: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with _started_app(tmp_path, database, monkeypatch) as gateway:
        gateway.handlers[0] = lambda request: _json_response(
            {
                "model": "deepseek-v4-pro",
                "choices": [{"message": {"content": "pong"}}],
                "usage": {"prompt_tokens": 40, "completion_tokens": 6},
            }
        )

        transport = httpx.ASGITransport(app=gateway.app)
        async with httpx.AsyncClient(transport=transport) as http_client:
            factory = gateway_llm_factory(
                settings=gateway.settings,
                sessions=database.sessions,
                price_table=gateway.app.state.services.prices,
                model="deepseek-v4-pro",
                http_client=http_client,
            )
            handle = await factory(_task(gateway, tmp_path))
            try:
                response = await handle.llm.complete(
                    messages=[LLMMessage(role="user", content="hi")]
                )
            finally:
                await handle.close()

        assert response.text == "pong"
        assert handle.usage.total().input_tokens == 40
        assert handle.usage.total().output_tokens == 6

        async with session_scope(database.sessions) as session:
            rows = await ScopedRepo(session, LlmUsageRow, gateway.workspace_id).list()
            assert len(rows) == 1
            assert rows[0].provider == "deepseek"
            assert rows[0].input_tokens == 40
            assert rows[0].output_tokens == 6


async def test_factory_revokes_the_run_token_on_close(
    tmp_path: Path,
    database: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with _started_app(tmp_path, database, monkeypatch) as gateway:
        factory = gateway_llm_factory(
            settings=gateway.settings,
            sessions=database.sessions,
            price_table=gateway.app.state.services.prices,
            model="deepseek-v4-pro",
        )
        handle = await factory(_task(gateway, tmp_path))
        await handle.close()

        async with session_scope(database.sessions) as session:
            rows = await ScopedRepo(session, RunToken, gateway.workspace_id).list()
            assert len(rows) == 1
            assert rows[0].revoked_at is not None


def test_factory_refuses_anthropic(tmp_path: Path) -> None:
    from gaia.services.costs import PriceTable

    with pytest.raises(ValueError, match="OpenAI-compatible"):
        gateway_llm_factory(
            settings=Settings(data_dir=tmp_path),
            sessions=None,  # type: ignore[arg-type]  # refused before any session opens
            price_table=PriceTable({}),
            model="claude-opus-5-5",
            provider="anthropic",
        )


async def test_factory_refuses_a_task_on_an_anthropic_model(tmp_path: Path) -> None:
    from gaia.services.costs import load_price_table

    factory = gateway_llm_factory(
        settings=Settings(data_dir=tmp_path),
        sessions=None,  # type: ignore[arg-type]  # refused before any session opens
        price_table=load_price_table(),
        model="deepseek-v4-pro",
    )
    task = AgentTask(
        task_id="t",
        run_id="r",
        run_step_id=None,
        workspace_id="w",
        agent_id=None,
        role=AgentRole.JUNIOR,
        instructions="x",
        worktree=WorktreeLocation(work_tree=tmp_path, git_dir=tmp_path / ".git"),
        base_sha="0" * 40,
        model="claude-sonnet-5-5",
    )

    with pytest.raises(ValueError, match="Claude runtime"):
        await factory(task)
