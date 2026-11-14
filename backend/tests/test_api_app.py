"""The app factory, its middleware and the error envelope (doc 04).

Every test here runs the real app through its lifespan (``conftest.api``), against
a throwaway SQLite file and an in-memory secret store. Nothing touches ``~/.gaia``
or the macOS Keychain.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import pytest
from fastapi import HTTPException
from sqlalchemy import select
from starlette.types import Message, Receive, Scope, Send

from gaia.api.app import (
    ALLOWED_HOSTS,
    RequestContextMiddleware,
    SecurityHeadersMiddleware,
    create_app,
)
from gaia.api.errors import (
    GENERIC_MESSAGE,
    INTERNAL_MESSAGE,
    REQUEST_ID_HEADER,
    ErrorCode,
)
from gaia.config.secrets import SecretName
from gaia.config.settings import Settings
from gaia.db.models import User, Workspace
from tests.conftest import API_BASE_URL, Api, Database


async def test_health_answers_without_a_token(api: Api) -> None:
    response = await api.client.get("/api/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


async def test_the_health_response_carries_the_security_headers(api: Api) -> None:
    response = await api.client.get("/api/health")

    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert response.headers["cache-control"] == "no-store"


async def test_an_api_token_is_required(api: Api) -> None:
    response = await api.client.get("/api/v1/me")

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
    assert response.json()["error"]["code"] == "unauthorized"
    assert response.json()["error"]["message"] == "a bearer token is required"


async def test_a_wrong_token_is_refused(api: Api) -> None:
    response = await api.client.get(
        "/api/v1/me", headers={"Authorization": "Bearer not-the-real-token"}
    )

    assert response.status_code == 401
    assert response.json()["error"]["message"] == "the bearer token is not valid"
    assert "not-the-real-token" not in response.text


async def test_a_valid_token_returns_the_principal(api: Api) -> None:
    response = await api.client.get("/api/v1/me", headers=api.auth)

    assert response.status_code == 200
    assert set(response.json()) == {"user_id", "workspace_id"}


async def test_the_principal_is_the_seeded_owner(api: Api, database: Database) -> None:
    """The token resolves to the workspace and user doc 03 seeded, not to constants."""
    body = (await api.client.get("/api/v1/me", headers=api.auth)).json()

    async with database.sessions() as session:
        user = (await session.scalars(select(User))).one()
        workspace = (await session.scalars(select(Workspace))).one()

    assert body == {"user_id": user.id, "workspace_id": workspace.id}


async def test_startup_seeds_one_workspace_and_one_owner(api: Api, database: Database) -> None:
    async with database.sessions() as session:
        workspaces = list((await session.scalars(select(Workspace))).all())
        users = list((await session.scalars(select(User))).all())

    assert [(workspace.slug, workspace.name) for workspace in workspaces] == [("local", "Local")]
    assert [user.email for user in users] == ["owner@localhost"]


async def test_startup_generates_a_token_on_first_run(api: Api) -> None:
    token = api.store.require(SecretName.GAIA_API_TOKEN).get_secret_value()

    # token_urlsafe(32) is 43 URL-safe characters.
    assert len(token) == 43


async def test_a_second_start_reuses_the_token(api: Api) -> None:
    """Restarting the server must not log the UI out, and must not reseed the DB."""
    first = api.token
    app = create_app(api.settings)

    async with app.router.lifespan_context(app):
        pass

    assert api.token == first


async def test_lifespan_cancels_and_awaits_background_tasks(api: Api) -> None:
    """Shutdown must not leave a dev fake-run publisher running."""
    app = create_app(api.settings)
    async with app.router.lifespan_context(app):
        task = asyncio.create_task(asyncio.sleep(60))
        app.state.services.background_tasks.add(task)

    assert task.cancelled()


async def test_startup_says_what_to_run_on_an_unmigrated_database(tmp_path: Path) -> None:
    """A fresh clone has no schema; the error names the command that creates one."""
    app = create_app(Settings(data_dir=tmp_path / "empty"))

    with pytest.raises(RuntimeError, match="make migrate"):
        async with app.router.lifespan_context(app):
            pass


async def test_a_foreign_host_header_is_refused(api: Api) -> None:
    """DNS rebinding: a page on evil.com resolving to 127.0.0.1 still sends evil.com."""
    response = await api.client.get("/api/health", headers={"Host": "evil.com"})

    assert response.status_code == 400
    # Starlette's own middleware answers this one; ours still decorate it.
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.parametrize("host", ALLOWED_HOSTS)
async def test_a_local_host_header_is_accepted(api: Api, host: str) -> None:
    response = await api.client.get("/api/health", headers={"Host": f"{host}:8765"})

    assert response.status_code == 200


async def test_a_cors_preflight_gets_no_permission(api: Api) -> None:
    """No CORS middleware: the UI is same-origin (doc 04 task 3, doc 16)."""
    response = await api.client.request(
        "OPTIONS",
        "/api/v1/me",
        headers={"Origin": "http://evil.example", "Access-Control-Request-Method": "GET"},
    )

    assert "access-control-allow-origin" not in response.headers
    assert response.status_code == 405
    assert response.json()["error"]["code"] == "method_not_allowed"


async def test_a_cross_origin_request_gets_no_cors_header(api: Api) -> None:
    response = await api.client.get(
        "/api/v1/me", headers={**api.auth, "Origin": "http://evil.example"}
    )

    assert response.status_code == 200
    assert "access-control-allow-origin" not in response.headers


async def test_an_unknown_api_path_is_an_envelope(api: Api) -> None:
    response = await api.client.get("/api/v1/nothing-here", headers=api.auth)

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


async def test_the_envelope_has_exactly_three_fields(api: Api) -> None:
    body = (await api.client.get("/api/v1/me")).json()

    assert set(body) == {"error"}
    assert set(body["error"]) == {"code", "message", "request_id"}


async def test_the_envelope_carries_the_request_id_header(api: Api) -> None:
    """The id in the body is the one in the log line for the same request."""
    response = await api.client.get("/api/v1/me")

    assert response.json()["error"]["request_id"] == response.headers[REQUEST_ID_HEADER]


async def test_a_request_id_is_generated_when_the_client_sends_none(api: Api) -> None:
    response = await api.client.get("/api/health")

    assert len(response.headers[REQUEST_ID_HEADER]) == 32


async def test_a_plausible_client_request_id_is_kept(api: Api) -> None:
    response = await api.client.get("/api/health", headers={REQUEST_ID_HEADER: "abc-123"})

    assert response.headers[REQUEST_ID_HEADER] == "abc-123"


@pytest.mark.parametrize("hostile", ["with a space", "semi;colon", "x" * 100, "<script>"])
async def test_an_implausible_client_request_id_is_replaced(api: Api, hostile: str) -> None:
    """The header is attacker-controlled input; it must not reach a log line raw."""
    response = await api.client.get("/api/health", headers={REQUEST_ID_HEADER: hostile})

    echoed = response.headers[REQUEST_ID_HEADER]
    assert echoed != hostile
    assert len(echoed) == 32


async def test_a_crash_never_returns_a_traceback(api: Api) -> None:
    """The body says nothing about what broke; the traceback goes to the log."""
    secret = "sk-ant-" + "a" * 32

    async def boom() -> None:
        raise RuntimeError(f"the provider key {secret} is wrong")

    app = create_app(api.settings)
    app.add_api_route("/api/v1/_boom", boom, methods=["GET"])
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=transport, base_url=API_BASE_URL) as client:
            response = await client.get("/api/v1/_boom", headers=api.auth)

    assert response.status_code == 500
    assert response.json()["error"]["code"] == "internal_error"
    assert response.json()["error"]["message"] == INTERNAL_MESSAGE
    assert "Traceback" not in response.text
    assert "RuntimeError" not in response.text
    assert secret not in response.text


async def test_a_validation_error_names_the_field_not_the_value(api: Api) -> None:
    """FastAPI's own 422 echoes the input; ours must not - a body can hold a token."""

    async def echo(limit: int) -> dict[str, int]:
        return {"limit": limit}

    app = create_app(api.settings)
    app.add_api_route("/api/v1/_echo", echo, methods=["GET"])
    transport = httpx.ASGITransport(app=app)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=transport, base_url=API_BASE_URL) as client:
            response = await client.get(
                "/api/v1/_echo", params={"limit": "not-a-number"}, headers=api.auth
            )

    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "validation_error"
    assert "limit" in error["message"]
    assert "not-a-number" not in response.text


async def test_the_openapi_document_declares_bearer_auth(api: Api) -> None:
    """Doc 06 exports this schema and doc 16 generates its types from it."""
    document = api.app.openapi()

    assert document["components"]["securitySchemes"]["bearer"] == {
        "type": "http",
        "scheme": "bearer",
    }
    assert document["paths"]["/api/v1/me"]["get"]["security"] == [{"bearer": []}]
    assert "security" not in document["paths"]["/api/health"]["get"]


@pytest.mark.parametrize("path", ["/docs", "/redoc", "/openapi.json"])
async def test_there_is_no_unauthenticated_documentation_route(api: Api, path: str) -> None:
    """Task 5: `/api/health` is the only route that answers without a token."""
    response = await api.client.get(path)

    assert response.status_code == 404


async def test_an_unknown_http_status_still_gets_an_envelope(api: Api) -> None:
    """A status with no registered code or phrase falls back instead of raising."""

    async def stubborn() -> None:
        raise HTTPException(status_code=599)

    app = create_app(api.settings)
    app.add_api_route("/api/v1/_odd", stubborn, methods=["GET"])
    transport = httpx.ASGITransport(app=app)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=transport, base_url=API_BASE_URL) as client:
            response = await client.get("/api/v1/_odd", headers=api.auth)

    assert response.status_code == 599
    assert response.json()["error"] == {
        "code": ErrorCode.HTTP_ERROR.value,
        "message": GENERIC_MESSAGE,
        "request_id": response.headers[REQUEST_ID_HEADER],
    }


async def test_a_non_string_error_detail_falls_back_to_a_message(api: Api) -> None:
    """A structured `detail` must not crash the handler (or reach `redact()`)."""

    async def structured() -> None:
        raise HTTPException(status_code=400, detail={"why": "structured"})

    app = create_app(api.settings)
    app.add_api_route("/api/v1/_structured", structured, methods=["GET"])
    transport = httpx.ASGITransport(app=app)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=transport, base_url=API_BASE_URL) as client:
            response = await client.get("/api/v1/_structured", headers=api.auth)

    assert response.status_code == 400
    assert response.json()["error"]["code"] == ErrorCode.BAD_REQUEST.value
    assert response.json()["error"]["message"] == GENERIC_MESSAGE


async def test_the_middleware_passes_a_non_http_scope_through() -> None:
    """A websocket (or lifespan) scope must reach the app untouched."""

    async def receive() -> Message:
        return {"type": "websocket.disconnect"}

    async def send(message: Message) -> None:
        raise AssertionError("a non-HTTP scope must not be answered")

    seen: list[str] = []

    async def inner(scope: Scope, receive: Receive, send: Send) -> None:
        seen.append(str(scope["type"]))

    scope: Scope = {"type": "websocket", "headers": []}
    await RequestContextMiddleware(inner)(scope, receive, send)
    await SecurityHeadersMiddleware(inner)(scope, receive, send)

    assert seen == ["websocket", "websocket"]
