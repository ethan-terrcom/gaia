"""The FastAPI app factory: one process, one database, one token.

Owned by doc 04. ``create_app(settings)`` is the only way to build the app: tests
pass their own :class:`~gaia.config.settings.Settings` (so nothing touches
``~/.gaia`` or the real keychain) and ``gaia serve`` passes none, which reads the
environment through :func:`~gaia.config.settings.get_settings`.

The startup order is the security order:

1. ``ensure_data_dir`` - ``~/.gaia`` exists and is ``0o700`` before any file does;
2. the engine and schema - a database that was never migrated fails here with the
   command that fixes it, instead of on the first request;
3. the seed - the workspace and local user every row hangs off (doc 03);
4. the token - generated and stored in the keychain on first run (doc 04), so the
   server can never come up in a state where nothing can authenticate.

Middleware order, outermost first: ``SecurityHeadersMiddleware``, then
``RequestContextMiddleware``, then :class:`TrustedHostMiddleware`. Starlette's
``add_middleware`` inserts at the front of the list, so the calls below run in the
opposite order. Security headers are outermost so even the plain 400 that
TrustedHostMiddleware answers for a spoofed ``Host`` carries them; the request id
is bound before the host is checked, so that 400 is traceable too.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from typing import Final

import httpx
import structlog
from fastapi import APIRouter, Depends, FastAPI, Security
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.datastructures import Headers, MutableHeaders
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.types import ASGIApp, Lifespan, Message, Receive, Scope, Send

import gaia
from gaia.api.auth import LocalTokenAuth, Principal, bearer_scheme
from gaia.api.deps import AppServices, get_principal
from gaia.api.errors import (
    REQUEST_ID_HEADER,
    REQUEST_ID_PATTERN,
    install_error_handlers,
    new_request_id,
)
from gaia.api.routes import agents, dev, health, repositories, session, stream, tickets, worktrees
from gaia.config.logging import configure_logging
from gaia.config.secrets import ensure_api_token, get_secret_store
from gaia.config.settings import Settings, ensure_data_dir, get_settings
from gaia.db.engine import create_engine_for, create_session_factory, session_scope
from gaia.db.seed import Seeded, seed_initial_data
from gaia.llm.gateway import router as llm_router
from gaia.services.costs import load_price_table
from gaia.services.events import EventBus
from gaia.services.git import GitService
from gaia.services.stream_tickets import StreamTickets

logger = structlog.get_logger(__name__)

#: The ``Host`` header a client may send. ``127.0.0.1`` is what the UI, Vite's
#: proxy and ``gaia ui`` use; ``localhost`` is there because a browser may send it.
#: This constrains the header, not the socket (doc 02's hand-off note).
ALLOWED_HOSTS: Final = ["127.0.0.1", "localhost"]

#: Where every JSON route lives. ``/api`` responses are never cached.
API_PREFIX: Final = "/api"

#: Added to every response, API or not.
SECURITY_HEADERS: Final = (
    ("X-Content-Type-Options", "nosniff"),
    ("Referrer-Policy", "no-referrer"),
)


class SecurityHeadersMiddleware:
    """Add the headers a browser needs and a cached response must not miss.

    ``Cache-Control: no-store`` is limited to ``/api``: the UI's own assets are
    static files (doc 16) and caching those is the point. ``setdefault`` rather
    than plain assignment, because doc 13's event stream sets its own caching
    policy on purpose.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path = scope.get("path", "")
        no_store = path == API_PREFIX or path.startswith(f"{API_PREFIX}/")

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                for name, value in SECURITY_HEADERS:
                    headers[name] = value
                if no_store:
                    headers.setdefault("Cache-Control", "no-store")
            await send(message)

        await self.app(scope, receive, send_with_headers)


class RequestContextMiddleware:
    """Give every request an id, echo it back, and put it in the log context.

    The id is what ties an error envelope the UI shows to a structlog line the
    owner greps, so it is bound before anything else can log. A client's own
    ``X-Request-ID`` is reused only when it looks like one: an unvalidated header
    would otherwise let a caller write arbitrary text into the logs.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        request_id = _inbound_request_id(scope) or new_request_id()
        scope.setdefault("state", {})["request_id"] = request_id
        # contextvars are per task; clearing first makes a reused worker task
        # start from a clean slate rather than inherit the previous request's id.
        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(request_id=request_id)

        async def send_with_request_id(message: Message) -> None:
            if message["type"] == "http.response.start":
                MutableHeaders(scope=message)[REQUEST_ID_HEADER] = request_id
            await send(message)

        try:
            await self.app(scope, receive, send_with_request_id)
        finally:
            structlog.contextvars.clear_contextvars()


def _inbound_request_id(scope: Scope) -> str | None:
    """Return the caller's request id, or ``None`` if it is missing or implausible."""
    candidate = Headers(scope=scope).get(REQUEST_ID_HEADER)
    if candidate is None:
        return None
    return candidate if REQUEST_ID_PATTERN.fullmatch(candidate) else None


async def _seed(sessions: async_sessionmaker[AsyncSession], settings: Settings) -> Seeded:
    """Create the default workspace and local owner, or say what to run first.

    SQLite creates an empty file on first connect, so an unmigrated database is
    the normal failure on a fresh clone. "no such table: workspace" is accurate
    and useless; the message names the command that fixes it.
    """
    try:
        async with session_scope(sessions) as db_session:
            return await seed_initial_data(db_session, settings)
    except OperationalError as error:
        raise RuntimeError(
            f"the database at {settings.db_url} has no schema yet: run `make migrate` ({error})"
        ) from error


def _lifespan(
    settings: Settings, llm_transport: httpx.AsyncBaseTransport | None
) -> Lifespan[FastAPI]:
    """Build the lifespan that opens the database, the LLM client and the token."""

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        configure_logging()
        ensure_data_dir(settings)
        git = GitService(settings)
        engine = create_engine_for(settings)
        sessions = create_session_factory(engine)
        prices = load_price_table()
        llm_http = httpx.AsyncClient(
            timeout=httpx.Timeout(connect=10, read=600, write=60, pool=10),
            transport=llm_transport,
        )
        try:
            seeded = await _seed(sessions, settings)
            principal = Principal(workspace_id=seeded.workspace.id, user_id=seeded.user.id)
            store = get_secret_store()
            ensure_api_token(store)
            events = EventBus(sessions)
            stream_tickets = StreamTickets()
            app.state.services = AppServices(
                settings=settings,
                engine=engine,
                sessions=sessions,
                store=store,
                auth=LocalTokenAuth(store, principal),
                principal=principal,
                git=git,
                events=events,
                stream_tickets=stream_tickets,
                llm_http=llm_http,
                prices=prices,
            )
            logger.info(
                "api.startup",
                host=settings.host,
                port=settings.port,
                workspace=seeded.workspace.slug,
                user=seeded.user.email,
            )
            yield
        finally:
            services: AppServices | None = getattr(app.state, "services", None)
            if services is not None:
                tasks = list(services.background_tasks)
                for task in tasks:
                    task.cancel()
                for task in tasks:
                    with suppress(asyncio.CancelledError):
                        await task
            await llm_http.aclose()
            # Closes the pool, so a second app in the same process (a test, or a
            # reload) does not inherit stale connections to the same file.
            await engine.dispose()

    return lifespan


def api_v1_router(*, dev_routes: bool = False) -> APIRouter:
    """Everything under ``/api/v1``: authenticated by default, not by memory.

    The auth dependency sits on the router, so a route added by doc 06 or 14 is
    protected even if its author forgets. ``bearer_scheme`` is documentation
    only (``auto_error=False``): it publishes the scheme to the OpenAPI document
    that doc 06 exports, while :func:`~gaia.api.deps.get_principal` does the one
    and only comparison.
    """
    router = APIRouter(
        prefix="/api/v1",
        dependencies=[Depends(get_principal), Security(bearer_scheme)],
    )
    router.include_router(session.router)
    router.include_router(repositories.router)
    router.include_router(tickets.router)
    router.include_router(agents.router)
    router.include_router(worktrees.router)
    router.include_router(stream.router)
    if dev_routes:
        router.include_router(dev.router)
    # TODO(gaia-14): include the runs and approvals routers.
    return router


def create_app(
    settings: Settings | None = None,
    *,
    llm_transport: httpx.AsyncBaseTransport | None = None,
) -> FastAPI:
    """Build the ASGI app.

    There is no interactive documentation route: ``/docs`` and ``/openapi.json``
    would be the only unauthenticated API routes besides ``/api/health``, and
    task 5 says there are none. The schema is exported offline instead
    (``create_app().openapi()``, doc 06).

    ``llm_transport`` is a test seam: production passes ``None`` and gets a real
    network client, while gateway tests pass ``httpx.MockTransport``.
    """
    resolved = settings if settings is not None else get_settings()
    app = FastAPI(
        title="Gaia",
        summary="Local-first control plane for a hierarchy of AI coding agents.",
        version=gaia.__version__,
        lifespan=_lifespan(resolved, llm_transport),
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    # add_middleware inserts at the front, so the last call here is the outermost
    # layer; see this module's docstring for the order and the reasons.
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=ALLOWED_HOSTS)
    app.add_middleware(RequestContextMiddleware)
    app.add_middleware(SecurityHeadersMiddleware)
    install_error_handlers(app)
    app.include_router(health.router, prefix=API_PREFIX)
    # The SSE endpoint is mounted outside ``api_v1_router()`` on purpose:
    # ``EventSource`` cannot send a bearer header, so the route authenticates by
    # the single-use ticket in the query string instead.
    app.include_router(stream.public_router)
    app.include_router(api_v1_router(dev_routes=resolved.dev_routes))
    # The gateway has its own run-token auth and therefore sits outside /api/v1.
    app.include_router(llm_router)
    return app
