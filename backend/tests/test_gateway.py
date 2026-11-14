"""Gateway routing, auth, budget enforcement and metering (doc 07b)."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest
import structlog
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.requests import Request
from starlette.types import Message, Scope

from gaia.api import app as app_module
from gaia.api.app import create_app
from gaia.config.secrets import InMemorySecretStore, MissingSecretError, SecretName
from gaia.config.settings import Settings
from gaia.db.engine import session_scope
from gaia.db.models import Event, Run
from gaia.db.models import LlmUsage as LlmUsageRow
from gaia.db.repo import EventRepo, ScopedRepo
from gaia.domain.enums import EventType
from gaia.llm import gateway as gateway_module
from gaia.llm.run_tokens import RunTokenContext, mint
from gaia.services.costs import BudgetExceeded, DbUsageRecorder
from tests.conftest import API_BASE_URL, Database
from tests.db_builders import Chain, make_chain

ANTHROPIC_KEY = "sk-ant-test-" + "x" * 40
DEEPSEEK_KEY = "sk-deepseek-test-" + "x" * 40
ADMIN_TOKEN = "admin-token-for-the-gateway-tests"


class ChunkedBody(httpx.AsyncByteStream):
    """An async response body that yields fixed chunks."""

    def __init__(self, chunks: list[bytes]) -> None:
        self.chunks = chunks

    async def __aiter__(self) -> Any:
        for chunk in self.chunks:
            yield chunk


@dataclass
class Gateway:
    """A live app whose LLM transport is a swappable ``httpx.MockTransport``."""

    client: httpx.AsyncClient
    app: FastAPI
    settings: Settings
    store: InMemorySecretStore
    chain: Chain
    anthropic_token: str
    deepseek_token: str
    handlers: list[Callable[[httpx.Request], httpx.Response]]

    @property
    def run_id(self) -> str:
        return self.chain.run.id

    @property
    def workspace_id(self) -> str:
        return self.chain.workspace.id

    def set_handler(self, handler: Callable[[httpx.Request], httpx.Response]) -> None:
        self.handlers[0] = handler


def _unexpected(request: httpx.Request) -> httpx.Response:
    raise AssertionError(f"unexpected upstream request: {request.method} {request.url}")


def _json_response(
    payload: dict[str, Any],
    *,
    status_code: int = 200,
    headers: dict[str, str] | None = None,
) -> httpx.Response:
    return httpx.Response(
        status_code,
        headers={"content-type": "application/json", **(headers or {})},
        stream=ChunkedBody([json.dumps(payload).encode("utf-8")]),
    )


def _direct_request(
    gateway: Gateway,
    *,
    method: str = "POST",
    path: str = "/llm/deepseek/chat/completions",
    body: bytes = b"{}",
    headers: dict[str, str] | None = None,
) -> Request:
    """Build a bare Starlette request for a direct ``_handle`` call."""

    async def receive() -> Message:
        return {"type": "http.request", "body": body, "more_body": False}

    raw_headers = [
        (name.lower().encode(), value.encode()) for name, value in (headers or {}).items()
    ]
    scope: Scope = {
        "type": "http",
        "method": method,
        "path": path,
        "headers": raw_headers,
        "query_string": b"",
        "server": ("127.0.0.1", 8765),
        "client": ("127.0.0.1", 12345),
        "scheme": "http",
        "root_path": "",
        "app": gateway.app,
    }
    return Request(scope, receive)


@asynccontextmanager
async def _fake_session_scope(factory: Any) -> AsyncIterator[object]:
    yield object()


class _FakeRun:
    workspace_id = "workspace"
    budget_usd = Decimal("5.0")
    cost_usd = Decimal("0")


class _FakeScopedRepo:
    def __init__(self, session: Any, model: Any, workspace_id: str) -> None:
        self.return_none = False

    async def get(self, row_id: str) -> _FakeRun | None:
        return None if self.return_none else _FakeRun()


def _direct_context(gateway: Gateway, *, provider: str = "deepseek") -> RunTokenContext:
    return RunTokenContext(
        token_id="token-direct",
        workspace_id=gateway.workspace_id,
        run_id=gateway.run_id,
        run_step_id=gateway.chain.step.id,
        agent_id=gateway.chain.agent.id,
        provider=provider,
    )


@pytest.fixture
async def gateway(
    tmp_path: Path, database: Database, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[Gateway]:
    """Start the real app on a throwaway DB with a mock LLM transport."""
    store = InMemorySecretStore()
    store.set(SecretName.GAIA_API_TOKEN, ADMIN_TOKEN)
    store.set(SecretName.ANTHROPIC_API_KEY, ANTHROPIC_KEY)
    store.set(SecretName.DEEPSEEK_API_KEY, DEEPSEEK_KEY)
    monkeypatch.setattr(app_module, "get_secret_store", lambda: store)

    settings = Settings(
        data_dir=tmp_path / "gaia-data",
        db_url=database.url,
        anthropic_base_url="https://anthropic.test",
        junior_base_url="https://deepseek.test",
    )
    handlers: list[Callable[[httpx.Request], httpx.Response]] = [_unexpected]
    app = create_app(
        settings, llm_transport=httpx.MockTransport(lambda request: handlers[0](request))
    )

    async with app.router.lifespan_context(app):
        async with session_scope(database.sessions) as session:
            chain = await make_chain(session, slug="gateway")
            runs = ScopedRepo(session, Run, chain.workspace.id)
            await runs.update(chain.run.id, budget_usd=Decimal("5.0"))
            anthropic_minted = await mint(
                session,
                workspace_id=chain.workspace.id,
                run_id=chain.run.id,
                provider="anthropic",
                ttl_s=3600,
                run_step_id=chain.step.id,
                agent_id=chain.agent.id,
            )
            deepseek_minted = await mint(
                session,
                workspace_id=chain.workspace.id,
                run_id=chain.run.id,
                provider="deepseek",
                ttl_s=3600,
                run_step_id=chain.step.id,
                agent_id=chain.agent.id,
            )

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url=API_BASE_URL
        ) as client:
            yield Gateway(
                client=client,
                app=app,
                settings=settings,
                store=store,
                chain=chain,
                anthropic_token=anthropic_minted.token.get_secret_value(),
                deepseek_token=deepseek_minted.token.get_secret_value(),
                handlers=handlers,
            )


async def _last_usage(session: AsyncSession, workspace_id: str) -> LlmUsageRow:
    rows = await ScopedRepo(session, LlmUsageRow, workspace_id).list()
    assert len(rows) == 1
    return rows[0]


async def test_missing_token_gets_provider_401(gateway: Gateway) -> None:
    response = await gateway.client.post(
        "/llm/deepseek/chat/completions",
        content=json.dumps({"model": "deepseek-v4-pro"}),
        headers={"content-type": "application/json"},
    )

    assert response.status_code == 401
    body = response.json()
    assert body["error"]["type"] == "authentication_error"
    assert DEEPSEEK_KEY not in response.text


async def test_admin_api_token_is_rejected_here(gateway: Gateway) -> None:
    response = await gateway.client.post(
        "/llm/deepseek/chat/completions",
        content=json.dumps({"model": "deepseek-v4-pro"}),
        headers={
            "content-type": "application/json",
            "Authorization": f"Bearer {ADMIN_TOKEN}",
        },
    )

    assert response.status_code == 401
    assert response.json()["error"]["type"] == "authentication_error"


async def test_run_token_is_rejected_on_api_v1(gateway: Gateway) -> None:
    response = await gateway.client.get(
        "/api/v1/me", headers={"Authorization": f"Bearer {gateway.deepseek_token}"}
    )

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthorized"


async def test_provider_mismatch_is_rejected(gateway: Gateway) -> None:
    response = await gateway.client.post(
        "/llm/anthropic/v1/messages",
        content=json.dumps({"model": "claude-opus-5-5", "max_tokens": 10}),
        headers={"x-api-key": gateway.deepseek_token},
    )

    assert response.status_code == 401
    assert response.json()["error"]["type"] == "authentication_error"


async def test_unknown_path_gets_provider_404(gateway: Gateway) -> None:
    response = await gateway.client.post(
        "/llm/deepseek/v1/unknown",
        content=json.dumps({"model": "deepseek-v4-pro"}),
        headers={"Authorization": f"Bearer {gateway.deepseek_token}"},
    )

    assert response.status_code == 404
    assert response.json()["error"]["type"] == "not_found_error"


async def test_non_post_gets_provider_405(gateway: Gateway) -> None:
    response = await gateway.client.get(
        "/llm/deepseek/chat/completions",
        headers={"Authorization": f"Bearer {gateway.deepseek_token}"},
    )

    assert response.status_code == 405
    assert response.json()["error"]["code"] == "invalid_request_error"


async def test_invalid_json_gets_provider_400(gateway: Gateway) -> None:
    response = await gateway.client.post(
        "/llm/deepseek/chat/completions",
        content=b"not-json",
        headers={"Authorization": f"Bearer {gateway.deepseek_token}"},
    )

    assert response.status_code == 400
    assert response.json()["error"]["type"] == "invalid_request_error"


async def test_non_object_json_gets_provider_400(gateway: Gateway) -> None:
    response = await gateway.client.post(
        "/llm/deepseek/chat/completions",
        content=b"[1, 2, 3]",
        headers={"Authorization": f"Bearer {gateway.deepseek_token}"},
    )

    assert response.status_code == 400
    assert response.json()["error"]["type"] == "invalid_request_error"


async def test_unpriced_model_gets_provider_400(gateway: Gateway) -> None:
    response = await gateway.client.post(
        "/llm/deepseek/chat/completions",
        content=json.dumps({"model": "not-priced"}),
        headers={"Authorization": f"Bearer {gateway.deepseek_token}"},
    )

    assert response.status_code == 400
    assert response.json()["error"]["type"] == "invalid_request_error"


async def test_missing_provider_key_gets_provider_503(gateway: Gateway) -> None:
    gateway.store.delete(SecretName.DEEPSEEK_API_KEY)

    response = await gateway.client.post(
        "/llm/deepseek/chat/completions",
        content=json.dumps({"model": "deepseek-v4-pro"}),
        headers={"Authorization": f"Bearer {gateway.deepseek_token}"},
    )

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "provider_not_configured"
    assert DEEPSEEK_KEY not in response.text


async def test_oversize_body_gets_provider_413(
    gateway: Gateway, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(gateway_module, "MAX_BODY_BYTES", 16)

    response = await gateway.client.post(
        "/llm/deepseek/chat/completions",
        content=b"x" * 32,
        headers={"Authorization": f"Bearer {gateway.deepseek_token}"},
    )

    assert response.status_code == 413
    assert response.json()["error"]["type"] == "invalid_request_error"


async def test_run_budget_refusal_is_403_without_upstream(
    gateway: Gateway, database: Database
) -> None:
    async with session_scope(database.sessions) as session:
        runs = ScopedRepo(session, Run, gateway.workspace_id)
        await runs.update(gateway.run_id, cost_usd=Decimal("5.0"))

    response = await gateway.client.post(
        "/llm/deepseek/chat/completions",
        content=json.dumps({"model": "deepseek-v4-pro"}),
        headers={"Authorization": f"Bearer {gateway.deepseek_token}"},
    )

    assert response.status_code == 403
    assert response.json()["error"]["type"] == "budget_exceeded"
    assert response.json()["error"]["code"] == "budget_exceeded"
    assert DEEPSEEK_KEY not in response.text

    async with session_scope(database.sessions) as session:
        events = await EventRepo(session, Event, gateway.workspace_id).list()
    assert [event.type for event in events] == ["run.budget_exceeded"]
    assert events[0].payload_json == {
        "kind": "run",
        "limit_usd": "5.000000",
        "spent_usd": "5.000000",
    }


async def test_daily_budget_refusal_records_daily_event(
    gateway: Gateway, database: Database
) -> None:
    async with session_scope(database.sessions) as session:
        usage_repo = ScopedRepo(session, LlmUsageRow, gateway.workspace_id)
        await usage_repo.create(
            run_id=gateway.run_id,
            provider="deepseek",
            model="deepseek-v4-pro",
            cost_usd=Decimal("20.0"),
        )

    response = await gateway.client.post(
        "/llm/deepseek/chat/completions",
        content=json.dumps({"model": "deepseek-v4-pro"}),
        headers={"Authorization": f"Bearer {gateway.deepseek_token}"},
    )

    assert response.status_code == 403
    assert DEEPSEEK_KEY not in response.text
    async with session_scope(database.sessions) as session:
        events = await EventRepo(session, Event, gateway.workspace_id).list()
    assert events[0].payload_json["kind"] == "daily"


async def test_streaming_passthrough_is_byte_identical_and_metered(
    gateway: Gateway, database: Database
) -> None:
    chunks = [
        b'data: {"model":"deepseek-v4-pro","choices":[{"delta":{"content":"hel"}}]}\n\n',
        b'data: {"model":"deepseek-v4-pro","usage":{"prompt_tokens":100,'
        b'"completion_tokens":9,"prompt_cache_hit_tokens":80,'
        b'"prompt_cache_miss_tokens":10}}\n\n',
        b"data: [DONE]\n\n",
    ]
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        seen["headers"] = dict(request.headers)
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream", "request-id": "req_stream"},
            stream=ChunkedBody(chunks),
        )

    gateway.set_handler(handler)
    response = await gateway.client.post(
        "/llm/deepseek/chat/completions",
        content=json.dumps({"model": "deepseek-v4-pro", "stream": True}),
        headers={"Authorization": f"Bearer {gateway.deepseek_token}"},
    )

    assert response.status_code == 200
    assert response.content == b"".join(chunks)
    assert seen["body"]["stream_options"]["include_usage"] is True

    async with session_scope(database.sessions) as session:
        usage = await _last_usage(session, gateway.workspace_id)
        assert usage.provider == "deepseek"
        assert usage.model == "deepseek-v4-pro"
        assert usage.input_tokens == 10
        assert usage.output_tokens == 9
        assert usage.cache_read_tokens == 80
        assert usage.run_step_id == gateway.chain.step.id
        assert usage.agent_id == gateway.chain.agent.id
        runs = ScopedRepo(session, Run, gateway.workspace_id)
        run = await runs.get(gateway.run_id)
        assert run is not None
        assert run.cost_usd == usage.cost_usd


async def test_non_streaming_deepseek_json_is_metered(gateway: Gateway, database: Database) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return _json_response(
            {
                "model": "deepseek-v4-pro",
                "choices": [{"message": {"content": "pong"}}],
                "usage": {"prompt_tokens": 40, "completion_tokens": 6},
            },
            headers={"request-id": "req_json"},
        )

    gateway.set_handler(handler)
    response = await gateway.client.post(
        "/llm/deepseek/chat/completions",
        content=json.dumps({"model": "deepseek-v4-pro"}),
        headers={"Authorization": f"Bearer {gateway.deepseek_token}"},
    )

    assert response.status_code == 200
    assert response.json()["model"] == "deepseek-v4-pro"

    async with session_scope(database.sessions) as session:
        usage = await _last_usage(session, gateway.workspace_id)
        assert usage.input_tokens == 40
        assert usage.output_tokens == 6
        assert usage.request_id == "req_json"


async def test_anthropic_non_streaming_json_is_metered(
    gateway: Gateway, database: Database
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return _json_response(
            {
                "model": "claude-opus-5-5",
                "content": [{"type": "text", "text": "pong"}],
                "usage": {
                    "input_tokens": 12,
                    "output_tokens": 3,
                    "cache_read_input_tokens": 4,
                    "cache_creation_input_tokens": 5,
                },
            },
            headers={"request-id": "req_anthropic"},
        )

    gateway.set_handler(handler)
    response = await gateway.client.post(
        "/llm/anthropic/v1/messages",
        content=json.dumps({"model": "claude-opus-5-5", "max_tokens": 10}),
        headers={"x-api-key": gateway.anthropic_token},
    )

    assert response.status_code == 200
    async with session_scope(database.sessions) as session:
        usage = await _last_usage(session, gateway.workspace_id)
        assert usage.provider == "anthropic"
        assert usage.model == "claude-opus-5-5"
        assert usage.input_tokens == 12
        assert usage.output_tokens == 3
        assert usage.cache_read_tokens == 4
        assert usage.cache_creation_tokens == 5


async def test_deepseek_anthropic_endpoint_uses_anthropic_parser(
    gateway: Gateway, database: Database
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return _json_response(
            {
                "model": "claude-opus-5-5",
                "usage": {"input_tokens": 7, "output_tokens": 2},
            },
            headers={"request-id": "req_deepseek_anthropic"},
        )

    gateway.set_handler(handler)
    response = await gateway.client.post(
        "/llm/deepseek/anthropic/v1/messages",
        content=json.dumps({"model": "claude-opus-5-5", "max_tokens": 10}),
        headers={"Authorization": f"Bearer {gateway.deepseek_token}"},
    )

    assert response.status_code == 200
    async with session_scope(database.sessions) as session:
        usage = await _last_usage(session, gateway.workspace_id)
        assert usage.provider == "deepseek"
        assert usage.input_tokens == 7
        assert usage.output_tokens == 2


async def test_count_tokens_is_not_metered(gateway: Gateway, database: Database) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return _json_response({"input_tokens": 8}, headers={"request-id": "req_count"})

    gateway.set_handler(handler)
    response = await gateway.client.post(
        "/llm/anthropic/v1/messages/count_tokens",
        content=json.dumps({"model": "claude-opus-5-5", "messages": []}),
        headers={"x-api-key": gateway.anthropic_token},
    )

    assert response.status_code == 200
    async with session_scope(database.sessions) as session:
        assert await ScopedRepo(session, LlmUsageRow, gateway.workspace_id).list() == []


async def test_upstream_error_is_relayed_without_recording(
    gateway: Gateway, database: Database
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return _json_response(
            {"error": {"message": "bad request"}},
            status_code=400,
            headers={"request-id": "req_upstream_error"},
        )

    gateway.set_handler(handler)
    response = await gateway.client.post(
        "/llm/deepseek/chat/completions",
        content=json.dumps({"model": "deepseek-v4-pro"}),
        headers={"Authorization": f"Bearer {gateway.deepseek_token}"},
    )

    assert response.status_code == 400
    assert response.json() == {"error": {"message": "bad request"}}
    assert DEEPSEEK_KEY not in response.text
    async with session_scope(database.sessions) as session:
        assert await ScopedRepo(session, LlmUsageRow, gateway.workspace_id).list() == []


async def test_upstream_connection_error_gets_provider_502(gateway: Gateway) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom", request=request)

    gateway.set_handler(handler)
    response = await gateway.client.post(
        "/llm/deepseek/chat/completions",
        content=json.dumps({"model": "deepseek-v4-pro"}),
        headers={"Authorization": f"Bearer {gateway.deepseek_token}"},
    )

    assert response.status_code == 502
    body = response.json()
    assert body["error"]["type"] == "api_error"
    assert "ConnectError" in body["error"]["message"]
    assert "boom" not in body["error"]["message"]
    assert DEEPSEEK_KEY not in response.text


async def test_request_headers_are_allowlisted_and_key_is_injected(
    gateway: Gateway, database: Database
) -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["headers"] = dict(request.headers)
        return _json_response(
            {
                "model": "deepseek-v4-pro",
                "choices": [{"message": {"content": "pong"}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            }
        )

    gateway.set_handler(handler)
    response = await gateway.client.post(
        "/llm/deepseek/chat/completions",
        content=json.dumps({"model": "deepseek-v4-pro"}),
        headers={
            "Authorization": f"Bearer {gateway.deepseek_token}",
            "content-type": "application/json",
            "accept": "application/json",
            "cookie": "session=secret",
            "x-forwarded-for": "1.2.3.4",
            "accept-encoding": "gzip",
        },
    )

    assert response.status_code == 200
    headers = seen["headers"]
    assert headers["authorization"] == f"Bearer {DEEPSEEK_KEY}"
    assert headers["accept-encoding"] == "identity"
    assert headers["content-type"] == "application/json"
    assert headers["accept"] == "application/json"
    assert "cookie" not in headers
    assert "x-forwarded-for" not in headers
    assert gateway.deepseek_token not in repr(headers)


async def test_anthropic_budget_refusal_uses_anthropic_403_shape(
    gateway: Gateway, database: Database
) -> None:
    async with session_scope(database.sessions) as session:
        runs = ScopedRepo(session, Run, gateway.workspace_id)
        await runs.update(gateway.run_id, cost_usd=Decimal("5.0"))

    response = await gateway.client.post(
        "/llm/anthropic/v1/messages",
        content=json.dumps({"model": "claude-opus-5-5", "max_tokens": 10}),
        headers={"x-api-key": gateway.anthropic_token},
    )

    assert response.status_code == 403
    assert response.json() == {
        "type": "error",
        "error": {"type": "permission_error", "message": "run LLM budget exceeded"},
    }


async def test_response_headers_are_allowlisted(gateway: Gateway) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return _json_response(
            {
                "model": "deepseek-v4-pro",
                "choices": [{"message": {"content": "pong"}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            },
            headers={
                "request-id": "req_headers",
                "retry-after": "3",
                "x-ratelimit-remaining": "5",
                "anthropic-ratelimit-requests-limit": "10",
                "set-cookie": "secret=1",
                "x-upstream-private": "no",
            },
        )

    gateway.set_handler(handler)
    response = await gateway.client.post(
        "/llm/deepseek/chat/completions",
        content=json.dumps({"model": "deepseek-v4-pro"}),
        headers={"Authorization": f"Bearer {gateway.deepseek_token}"},
    )

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/json"
    assert response.headers["request-id"] == "req_headers"
    assert response.headers["retry-after"] == "3"
    assert response.headers["x-ratelimit-remaining"] == "5"
    assert "set-cookie" not in response.headers
    assert "x-upstream-private" not in response.headers


@pytest.mark.parametrize("payload", [{"messages": []}, {"model": 123}])
async def test_model_must_be_a_non_empty_string(gateway: Gateway, payload: dict[str, Any]) -> None:
    response = await gateway.client.post(
        "/llm/deepseek/chat/completions",
        content=json.dumps(payload),
        headers={"Authorization": f"Bearer {gateway.deepseek_token}"},
    )

    assert response.status_code == 400
    assert response.json()["error"]["type"] == "invalid_request_error"


async def test_a_2xx_without_usage_records_a_conservative_estimate(
    gateway: Gateway, database: Database
) -> None:
    """Lead change (review 07b, G2): missing usage is estimated high, never free."""
    request_body = json.dumps({"model": "deepseek-v4-pro"})

    def handler(request: httpx.Request) -> httpx.Response:
        return _json_response({"model": "deepseek-v4-pro", "choices": []})

    gateway.set_handler(handler)
    response = await gateway.client.post(
        "/llm/deepseek/chat/completions",
        content=request_body,
        headers={"Authorization": f"Bearer {gateway.deepseek_token}"},
    )

    assert response.status_code == 200
    async with session_scope(database.sessions) as session:
        usage = await _last_usage(session, gateway.workspace_id)
        assert usage.input_tokens == -(-len(request_body) // 3)
        assert usage.output_tokens == -(-len(response.content) // 3)
        assert usage.price_tier == "estimated"
        runs = ScopedRepo(session, Run, gateway.workspace_id)
        run = await runs.get(gateway.run_id)
        assert run is not None
        assert run.cost_usd == usage.cost_usd > 0


async def test_metering_failure_does_not_break_the_stream(
    gateway: Gateway, database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return _json_response(
            {
                "model": "deepseek-v4-pro",
                "choices": [{"message": {"content": "pong"}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            }
        )

    async def fail_record(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("metering failure")

    monkeypatch.setattr(DbUsageRecorder, "record", fail_record)
    gateway.set_handler(handler)
    response = await gateway.client.post(
        "/llm/deepseek/chat/completions",
        content=json.dumps({"model": "deepseek-v4-pro"}),
        headers={"Authorization": f"Bearer {gateway.deepseek_token}"},
    )

    assert response.status_code == 200
    async with session_scope(database.sessions) as session:
        assert await ScopedRepo(session, LlmUsageRow, gateway.workspace_id).list() == []


async def test_upstream_stream_error_ends_the_stream_cleanly(
    gateway: Gateway, database: Database
) -> None:
    class ExplodingBody(httpx.AsyncByteStream):
        async def __aiter__(self) -> Any:
            yield b"partial"
            raise httpx.ReadTimeout("timeout")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream", "request-id": "req_stream_error"},
            stream=ExplodingBody(),
        )

    gateway.set_handler(handler)
    response = await gateway.client.post(
        "/llm/deepseek/chat/completions",
        content=json.dumps({"model": "deepseek-v4-pro", "stream": True}),
        headers={"Authorization": f"Bearer {gateway.deepseek_token}"},
    )

    assert response.status_code == 200
    assert response.content == b"partial"
    async with session_scope(database.sessions) as session:
        usage = await _last_usage(session, gateway.workspace_id)
        assert usage.price_tier == "estimated"


async def test_upstream_close_error_is_logged_and_does_not_break_the_stream(
    gateway: Gateway, database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    class ExplodingBody(httpx.AsyncByteStream):
        async def __aiter__(self) -> Any:
            yield b"partial"
            raise httpx.ReadTimeout("timeout")

    def handler(request: httpx.Request) -> httpx.Response:
        response = httpx.Response(
            200,
            headers={"content-type": "text/event-stream", "request-id": "req_close_error"},
            stream=ExplodingBody(),
        )

        async def bad_close() -> None:
            raise RuntimeError("close boom")

        monkeypatch.setattr(response, "aclose", bad_close)
        return response

    gateway.set_handler(handler)
    response = await gateway.client.post(
        "/llm/deepseek/chat/completions",
        content=json.dumps({"model": "deepseek-v4-pro", "stream": True}),
        headers={"Authorization": f"Bearer {gateway.deepseek_token}"},
    )

    assert response.status_code == 200
    assert response.content == b"partial"
    async with session_scope(database.sessions) as session:
        usage = await _last_usage(session, gateway.workspace_id)
        assert usage.price_tier == "estimated"


async def test_handle_direct_rejects_provider_mismatch(
    gateway: Gateway, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_resolve(session: Any, presented: str | None) -> RunTokenContext:
        return _direct_context(gateway, provider="deepseek")

    monkeypatch.setattr(gateway_module, "session_scope", _fake_session_scope)
    monkeypatch.setattr(gateway_module, "resolve_run_token", fake_resolve)

    request = _direct_request(
        gateway,
        path="/llm/anthropic/v1/messages",
        body=json.dumps({"model": "claude-opus-5-5"}).encode(),
        headers={"x-api-key": gateway.anthropic_token},
    )
    response = await gateway_module._handle(request, "anthropic", "v1/messages")

    assert response.status_code == 401


async def test_handle_direct_rejects_non_post(
    gateway: Gateway, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_resolve(session: Any, presented: str | None) -> RunTokenContext:
        return _direct_context(gateway)

    monkeypatch.setattr(gateway_module, "session_scope", _fake_session_scope)
    monkeypatch.setattr(gateway_module, "resolve_run_token", fake_resolve)

    request = _direct_request(gateway, method="GET")
    response = await gateway_module._handle(request, "deepseek", "chat/completions")

    assert response.status_code == 405


async def test_handle_direct_rejects_unknown_path(
    gateway: Gateway, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_resolve(session: Any, presented: str | None) -> RunTokenContext:
        return _direct_context(gateway)

    monkeypatch.setattr(gateway_module, "session_scope", _fake_session_scope)
    monkeypatch.setattr(gateway_module, "resolve_run_token", fake_resolve)

    request = _direct_request(gateway, path="/llm/deepseek/v1/unknown")
    response = await gateway_module._handle(request, "deepseek", "v1/unknown")

    assert response.status_code == 404


async def test_handle_direct_rejects_oversize_body(
    gateway: Gateway, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_resolve(session: Any, presented: str | None) -> RunTokenContext:
        return _direct_context(gateway)

    monkeypatch.setattr(gateway_module, "session_scope", _fake_session_scope)
    monkeypatch.setattr(gateway_module, "resolve_run_token", fake_resolve)
    monkeypatch.setattr(gateway_module, "MAX_BODY_BYTES", 16)

    request = _direct_request(gateway, body=b"x" * 32)
    response = await gateway_module._handle(request, "deepseek", "chat/completions")

    assert response.status_code == 413


async def test_handle_direct_rejects_invalid_json(
    gateway: Gateway, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_resolve(session: Any, presented: str | None) -> RunTokenContext:
        return _direct_context(gateway)

    monkeypatch.setattr(gateway_module, "session_scope", _fake_session_scope)
    monkeypatch.setattr(gateway_module, "resolve_run_token", fake_resolve)

    request = _direct_request(gateway, body=b"not-json")
    response = await gateway_module._handle(request, "deepseek", "chat/completions")

    assert response.status_code == 400


async def test_handle_direct_rejects_unpriced_model(
    gateway: Gateway, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_resolve(session: Any, presented: str | None) -> RunTokenContext:
        return _direct_context(gateway)

    monkeypatch.setattr(gateway_module, "session_scope", _fake_session_scope)
    monkeypatch.setattr(gateway_module, "resolve_run_token", fake_resolve)

    request = _direct_request(gateway, body=json.dumps({"model": "not-priced"}).encode())
    response = await gateway_module._handle(request, "deepseek", "chat/completions")

    assert response.status_code == 400


async def test_handle_direct_rejects_missing_run(
    gateway: Gateway, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_resolve(session: Any, presented: str | None) -> RunTokenContext:
        return _direct_context(gateway)

    class MissingRunRepo:
        def __init__(self, session: Any, model: Any, workspace_id: str) -> None:
            pass

        async def get(self, row_id: str) -> None:
            return None

    monkeypatch.setattr(gateway_module, "session_scope", _fake_session_scope)
    monkeypatch.setattr(gateway_module, "resolve_run_token", fake_resolve)
    monkeypatch.setattr(gateway_module, "ScopedRepo", MissingRunRepo)

    request = _direct_request(gateway, body=json.dumps({"model": "deepseek-v4-pro"}).encode())
    response = await gateway_module._handle(request, "deepseek", "chat/completions")

    assert response.status_code == 401


async def test_handle_direct_budget_refusal_publishes_on_the_bus(
    gateway: Gateway, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_resolve(session: Any, presented: str | None) -> RunTokenContext:
        return _direct_context(gateway)

    async def fake_ensure_budget(session: Any, run: Any, settings: Settings, *, now: Any) -> None:
        raise BudgetExceeded("run", Decimal("5.0"), Decimal("5.0"))

    published: list[dict[str, Any]] = []

    async def fake_publish(**kwargs: Any) -> None:
        published.append(kwargs)

    monkeypatch.setattr(gateway_module, "session_scope", _fake_session_scope)
    monkeypatch.setattr(gateway_module, "resolve_run_token", fake_resolve)
    monkeypatch.setattr(gateway_module, "ScopedRepo", _FakeScopedRepo)
    monkeypatch.setattr(gateway_module, "ensure_budget", fake_ensure_budget)
    monkeypatch.setattr(gateway.app.state.services.events, "publish", fake_publish)

    request = _direct_request(
        gateway,
        body=json.dumps({"model": "deepseek-v4-pro", "stream": True}).encode(),
    )
    response = await gateway_module._handle(request, "deepseek", "chat/completions")

    assert response.status_code == 403
    assert len(published) == 1
    assert published[0]["type"] is EventType.RUN_BUDGET_EXCEEDED
    assert published[0]["run_id"] == gateway.run_id
    assert published[0]["payload"] == {"kind": "run", "limit_usd": "5.0", "spent_usd": "5.0"}


async def test_handle_direct_budget_event_failure_still_refuses(
    gateway: Gateway, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_resolve(session: Any, presented: str | None) -> RunTokenContext:
        return _direct_context(gateway)

    async def fake_ensure_budget(session: Any, run: Any, settings: Settings, *, now: Any) -> None:
        raise BudgetExceeded("daily", Decimal("20.0"), Decimal("20.0"))

    async def failing_publish(**kwargs: Any) -> None:
        raise RuntimeError("bus down")

    monkeypatch.setattr(gateway_module, "session_scope", _fake_session_scope)
    monkeypatch.setattr(gateway_module, "resolve_run_token", fake_resolve)
    monkeypatch.setattr(gateway_module, "ScopedRepo", _FakeScopedRepo)
    monkeypatch.setattr(gateway_module, "ensure_budget", fake_ensure_budget)
    monkeypatch.setattr(gateway.app.state.services.events, "publish", failing_publish)

    request = _direct_request(
        gateway,
        body=json.dumps(
            {"model": "deepseek-v4-pro", "stream": True, "stream_options": "bad"}
        ).encode(),
    )
    response = await gateway_module._handle(request, "deepseek", "chat/completions")

    assert response.status_code == 403
    assert b"budget_exceeded" in bytes(response.body)


async def test_handle_direct_prepares_body_then_returns_503_for_missing_key(
    gateway: Gateway, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_resolve(session: Any, presented: str | None) -> RunTokenContext:
        return _direct_context(gateway)

    async def fake_ensure_budget(session: Any, run: Any, settings: Settings, *, now: Any) -> None:
        return None

    async def fake_run_in_threadpool(func: Any, *args: Any) -> None:
        raise MissingSecretError(SecretName.DEEPSEEK_API_KEY)

    monkeypatch.setattr(gateway_module, "session_scope", _fake_session_scope)
    monkeypatch.setattr(gateway_module, "resolve_run_token", fake_resolve)
    monkeypatch.setattr(gateway_module, "ScopedRepo", _FakeScopedRepo)
    monkeypatch.setattr(gateway_module, "ensure_budget", fake_ensure_budget)
    monkeypatch.setattr(gateway_module, "run_in_threadpool", fake_run_in_threadpool)

    request = _direct_request(gateway, body=json.dumps({"model": "deepseek-v4-pro"}).encode())
    response = await gateway_module._handle(request, "deepseek", "chat/completions")

    assert response.status_code == 503


async def test_gateway_logs_metadata_only_and_never_the_key(
    gateway: Gateway, database: Database
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return _json_response(
            {
                "model": "deepseek-v4-pro",
                "choices": [{"message": {"content": "pong"}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            },
            headers={"request-id": "req_log"},
        )

    gateway.set_handler(handler)
    with structlog.testing.capture_logs() as captured:
        response = await gateway.client.post(
            "/llm/deepseek/chat/completions",
            content=json.dumps({"model": "deepseek-v4-pro"}),
            headers={"Authorization": f"Bearer {gateway.deepseek_token}"},
        )

    assert response.status_code == 200
    assert DEEPSEEK_KEY not in response.text
    gateway_line = [entry for entry in captured if entry.get("event") == "llm.gateway"]
    assert gateway_line
    rendered = repr(gateway_line)
    assert "run_id" in rendered
    assert DEEPSEEK_KEY not in rendered
    assert gateway.deepseek_token not in rendered


class StallingBody(httpx.AsyncByteStream):
    """Yield some chunks, then stall until cancelled (a slow model mid-answer)."""

    def __init__(self, chunks: list[bytes]) -> None:
        self.chunks = chunks

    async def __aiter__(self) -> Any:
        for chunk in self.chunks:
            yield chunk
        await asyncio.Event().wait()


async def _disconnect_mid_stream(app: FastAPI, path: str, body: bytes, token: str) -> list[Message]:
    """Drive the ASGI app directly: send the request, then disconnect after the first chunk."""
    first_chunk_sent = asyncio.Event()
    sent: list[Message] = []
    delivered = False

    async def receive() -> Message:
        nonlocal delivered
        if not delivered:
            delivered = True
            return {"type": "http.request", "body": body, "more_body": False}
        await first_chunk_sent.wait()
        return {"type": "http.disconnect"}

    async def send(message: Message) -> None:
        sent.append(message)
        if message["type"] == "http.response.body" and message.get("body"):
            first_chunk_sent.set()

    scope: Scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "root_path": "",
        "headers": [
            (b"host", b"127.0.0.1"),
            (b"content-type", b"application/json"),
            (b"x-api-key", token.encode()),
        ],
        "client": ("127.0.0.1", 50000),
        "server": ("127.0.0.1", 8765),
        "state": {},
    }
    await asyncio.wait_for(app(scope, receive, send), timeout=10)
    return sent


async def test_a_client_disconnect_mid_stream_still_records_usage(
    gateway: Gateway, database: Database
) -> None:
    """Lead review 07b (G1): an interrupted stream must still be charged.

    Otherwise a harness that disconnects mid-answer (an interrupt, a timeout)
    spends money the budget never sees.
    """
    message_start = (
        b"event: message_start\n"
        b'data: {"type":"message_start","message":{"model":"claude-opus-5-5",'
        b'"usage":{"input_tokens":1000,"output_tokens":1}}}\n\n'
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            stream=StallingBody([message_start]),
        )

    gateway.set_handler(handler)
    await _disconnect_mid_stream(
        gateway.app,
        "/llm/anthropic/v1/messages",
        json.dumps({"model": "claude-opus-5-5", "stream": True, "max_tokens": 10}).encode(),
        gateway.anthropic_token,
    )

    async with session_scope(database.sessions) as session:
        usage = await _last_usage(session, gateway.workspace_id)
        assert usage.input_tokens == 1000
        assert usage.cost_usd > 0
