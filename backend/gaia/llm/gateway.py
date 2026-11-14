"""The LLM gateway: a provider-shaped reverse proxy with per-run auth.

Doc 07b moves provider keys out of agent processes. A harness points its base
URL at this router and presents a per-run token; the gateway resolves the token,
enforces budgets, injects the real key from the keychain, and streams the
provider response back unchanged while metering its usage.

This router sits outside ``/api/v1`` on purpose: it speaks the providers' error
shapes, not Gaia's API envelope, because its callers are Anthropic/OpenAI
clients rather than the UI.
"""

from __future__ import annotations

import json
import time
from collections.abc import AsyncIterator
from datetime import datetime
from typing import Any, Literal

import anyio
import httpx
import structlog
from fastapi import APIRouter, Request
from pydantic import SecretStr
from starlette.concurrency import run_in_threadpool
from starlette.responses import JSONResponse, Response, StreamingResponse

from gaia.api.auth import bearer_token
from gaia.api.deps import AppServices
from gaia.config.secrets import MissingSecretError, SecretName
from gaia.db.engine import session_scope
from gaia.db.models import Run
from gaia.db.repo import ScopedRepo
from gaia.db.types import utcnow
from gaia.domain.enums import EventType
from gaia.llm.metering import UsageAccumulator, estimate_usage
from gaia.llm.run_tokens import RunTokenContext
from gaia.llm.run_tokens import resolve as resolve_run_token
from gaia.services.costs import BudgetExceeded, DbUsageRecorder, PriceTable, ensure_budget

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/llm")

#: Largest request body the gateway will parse and forward.
MAX_BODY_BYTES = 20 * 1024 * 1024

#: Upper bound on the shielded cleanup after a stream ends (close upstream,
#: record usage). Shielded so a client disconnect cannot cancel the charge;
#: bounded so a stuck database cannot hold a worker forever.
RECORD_TIMEOUT_S = 10.0

#: Provider endpoints this package knows how to forward.
ANTHROPIC_ALLOWLIST = frozenset({"v1/messages", "v1/messages/count_tokens"})
DEEPSEEK_ALLOWLIST = frozenset({"chat/completions", "v1/chat/completions", "anthropic/v1/messages"})

#: Request headers that are forwarded upstream. Everything else is dropped,
#: including the client's own credential headers.
FORWARDED_REQUEST_HEADERS = frozenset(
    {"content-type", "accept", "anthropic-version", "anthropic-beta", "user-agent"}
)

#: Response headers copied back to the client.
EXACT_RESPONSE_HEADERS = frozenset({"content-type", "request-id", "x-request-id", "retry-after"})
RATELIMIT_HEADER_PREFIXES = ("anthropic-ratelimit-", "x-ratelimit-")


def _now() -> datetime:
    """Return the current aware UTC time (a function so tests can patch it)."""
    return utcnow()


def _normalise_path(path: str) -> str:
    """Return ``path`` with no leading or trailing slash."""
    return path.strip("/")


def _presented_token(request: Request) -> str | None:
    """Read the run token from ``x-api-key`` or ``Authorization: Bearer``."""
    api_key = request.headers.get("x-api-key")
    if api_key:
        return api_key
    return bearer_token(request)


def _secret_name(provider: str) -> SecretName:
    """The keychain entry for one provider."""
    return SecretName.ANTHROPIC_API_KEY if provider == "anthropic" else SecretName.DEEPSEEK_API_KEY


def _provider_error(
    provider: str,
    status_code: int,
    error_type: str,
    message: str,
    *,
    code: str | None = None,
) -> JSONResponse:
    """Build an error in the provider's own shape, never Gaia's envelope."""
    if provider == "anthropic":
        body: dict[str, Any] = {"type": "error", "error": {"type": error_type, "message": message}}
    else:
        body = {"error": {"message": message, "type": error_type, "code": code or error_type}}
    return JSONResponse(body, status_code=status_code)


def _budget_exceeded_response(provider: str, exc: BudgetExceeded) -> JSONResponse:
    """The non-retryable 403 shape each harness knows to stop on."""
    message = f"{exc.kind} LLM budget exceeded"
    if provider == "anthropic":
        return _provider_error(provider, 403, "permission_error", message)
    return _provider_error(provider, 403, "budget_exceeded", message, code="budget_exceeded")


def _upstream_headers(request: Request, provider: str, key: SecretStr) -> dict[str, str]:
    """Build upstream headers: only the allowlist plus the injected real key."""
    headers: dict[str, str] = {}
    for name in FORWARDED_REQUEST_HEADERS:
        value = request.headers.get(name)
        if value is not None:
            headers[name] = value
    # The relayed bytes are the bytes that are metered; never let httpx decode a
    # compressed stream differently.
    headers["accept-encoding"] = "identity"
    if provider == "anthropic":
        headers["x-api-key"] = key.get_secret_value()
    else:
        headers["authorization"] = f"Bearer {key.get_secret_value()}"
    return headers


def _response_headers(upstream: httpx.Response) -> dict[str, str]:
    """Copy the small allowlist of response headers downstream."""
    headers: dict[str, str] = {}
    for name in EXACT_RESPONSE_HEADERS:
        value = upstream.headers.get(name)
        if value is not None:
            headers[name] = value
    for name, value in upstream.headers.items():
        if any(name.startswith(prefix) for prefix in RATELIMIT_HEADER_PREFIXES):
            headers[name] = value
    return headers


def _upstream_request_id(headers: httpx.Headers) -> str | None:
    """Read the provider request id from the response headers, if present."""
    request_id: str | None = headers.get("request-id")
    if request_id is not None:
        return request_id
    fallback: str | None = headers.get("x-request-id")
    return fallback


def _log_call(
    *,
    run_id: str | None,
    provider: str,
    path: str,
    model: str | None,
    status: int,
    duration_ms: float,
    upstream_request_id: str | None,
    streamed: bool,
) -> None:
    """One metadata-only log line per gateway call (never headers or bodies)."""
    logger.info(
        "llm.gateway",
        run_id=run_id,
        provider=provider,
        path=path,
        model=model,
        status=status,
        duration_ms=round(duration_ms, 1),
        upstream_request_id=upstream_request_id,
        streamed=streamed,
    )


def _allowlist_for(provider: str) -> frozenset[str]:
    """The normalised paths this gateway forwards for ``provider``."""
    return ANTHROPIC_ALLOWLIST if provider == "anthropic" else DEEPSEEK_ALLOWLIST


def _parser_for(provider: str, path: str) -> Literal["anthropic", "openai"]:
    """Choose the usage parser for this provider and path."""
    if provider == "anthropic" or path == "anthropic/v1/messages":
        return "anthropic"
    return "openai"


async def _read_body(request: Request) -> tuple[bytes | None, JSONResponse | None]:
    """Read the request body, capped at :data:`MAX_BODY_BYTES`."""
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > MAX_BODY_BYTES:
            return None, _provider_error(
                "anthropic", 413, "invalid_request_error", "request body is too large"
            )
    return bytes(body), None


def _parse_request_json(raw: bytes) -> tuple[dict[str, Any] | None, JSONResponse | None]:
    """Parse the request body as one JSON object."""
    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None, _provider_error(
            "anthropic", 400, "invalid_request_error", "request body is not valid JSON"
        )
    if not isinstance(payload, dict):
        return None, _provider_error(
            "anthropic", 400, "invalid_request_error", "request body must be a JSON object"
        )
    return payload, None


def _validate_model(
    payload: dict[str, Any], prices: PriceTable
) -> tuple[str | None, JSONResponse | None]:
    """Return the request model, or a 400 error when it is not priced."""
    model = payload.get("model")
    if not isinstance(model, str) or not model:
        return None, _provider_error(
            "anthropic", 400, "invalid_request_error", "the request model is not configured"
        )
    try:
        prices.require(model)
    except KeyError:
        return None, _provider_error(
            "anthropic", 400, "invalid_request_error", "the request model is not configured"
        )
    return model, None


async def _publish_budget_event(
    services: AppServices, context: RunTokenContext, exc: BudgetExceeded
) -> None:
    """Tell the UI a call was refused for budget (doc 13's bus: stored and streamed).

    Called after the request's own session is closed, so the bus's short write
    transaction never waits on it. A failure is logged, not raised: the 403 is
    what stops the harness, the event only explains it.
    """
    try:
        await services.events.publish(
            workspace_id=context.workspace_id,
            type=EventType.RUN_BUDGET_EXCEEDED,
            payload={
                "kind": exc.kind,
                "limit_usd": str(exc.limit_usd),
                "spent_usd": str(exc.spent_usd),
            },
            run_id=context.run_id,
            agent_id=context.agent_id,
        )
    except Exception:
        logger.warning("llm.gateway_budget_event_failed", run_id=context.run_id, exc_info=True)


async def _record_usage(
    *,
    status_code: int,
    accumulator: UsageAccumulator,
    context: RunTokenContext,
    services: AppServices,
    requested_model: str,
    provider: str,
    path: str,
    request_id: str | None,
    request_bytes: int,
    response_bytes: int,
) -> None:
    """Record one gateway call in a fresh session after the stream ends.

    When the provider's usage block never arrived (an interrupted stream), a
    conservative estimate is recorded with ``price_tier="estimated"`` instead
    of a free call, so the budget still sees the spend (review 07b, G2).
    """
    if not 200 <= status_code < 300:
        return
    try:
        usage = accumulator.finish(request_id=request_id)
        estimated = usage is None
        if usage is None:
            usage = estimate_usage(
                provider=provider,
                model=requested_model,
                request_bytes=request_bytes,
                response_bytes=response_bytes,
            )
            logger.warning(
                "llm.gateway_usage_estimated",
                run_id=context.run_id,
                provider=provider,
                path=path,
                model=requested_model,
            )

        async with session_scope(services.sessions) as session:
            recorder = DbUsageRecorder(
                session,
                context.workspace_id,
                context.run_id,
                run_step_id=context.run_step_id,
                agent_id=context.agent_id,
            )
            quote = services.prices.quote_or_price_as(
                usage, at=_now(), fallback_model=requested_model
            )
            tier = "estimated" if estimated else quote.price_tier
            await recorder.record(usage, cost_usd=quote.cost_usd, price_tier=tier)
    except Exception:
        # The stream has already been sent; a metering failure must not turn a
        # successful model call into a broken response.
        logger.warning(
            "llm.gateway_record_failed",
            run_id=context.run_id,
            provider=provider,
            path=path,
            exc_info=True,
        )


def _relay_response(
    upstream: httpx.Response,
    accumulator: UsageAccumulator | None,
    *,
    context: RunTokenContext,
    services: AppServices,
    requested_model: str,
    provider: str,
    path: str,
    request_id: str | None,
    meter: bool,
    request_bytes: int,
) -> StreamingResponse:
    """Stream ``upstream`` downstream unchanged, metering and closing it after.

    The cleanup in ``finally`` runs in a **shielded** cancel scope. When the
    client disconnects, Starlette cancels this generator through an anyio
    cancel scope, and without the shield every ``await`` in ``finally`` would be
    cancelled too, so the call would never be charged (review 07b, G1).
    """
    relayed = 0

    async def body() -> AsyncIterator[bytes]:
        nonlocal relayed
        try:
            async for chunk in upstream.aiter_raw():
                if accumulator is not None:
                    accumulator.feed(chunk)
                relayed += len(chunk)
                yield chunk
        except httpx.TransportError as exc:
            # The status line is already on the wire; log the class only and end
            # the stream cleanly.
            logger.warning(
                "llm.gateway_upstream_stream_error",
                run_id=context.run_id,
                provider=provider,
                path=path,
                exception_class=type(exc).__name__,
            )
        finally:
            with anyio.CancelScope(shield=True), anyio.move_on_after(RECORD_TIMEOUT_S):
                try:
                    await upstream.aclose()
                except Exception:
                    logger.warning(
                        "llm.gateway_upstream_close_error",
                        run_id=context.run_id,
                        provider=provider,
                        path=path,
                        exc_info=True,
                    )
                if meter and accumulator is not None:
                    await _record_usage(
                        status_code=upstream.status_code,
                        accumulator=accumulator,
                        context=context,
                        services=services,
                        requested_model=requested_model,
                        provider=provider,
                        path=path,
                        request_id=request_id,
                        request_bytes=request_bytes,
                        response_bytes=relayed,
                    )

    return StreamingResponse(
        body(),
        status_code=upstream.status_code,
        headers=_response_headers(upstream),
    )


async def _handle(request: Request, provider: str, path: str) -> Response:
    """Resolve, budget-check and forward one gateway call."""
    start = time.perf_counter()
    services: AppServices = request.app.state.services
    normalised = _normalise_path(path)
    presented = _presented_token(request)
    context: RunTokenContext | None = None
    requested_model: str | None = None
    streamed = False
    parser: Literal["anthropic", "openai"] = _parser_for(provider, normalised)

    async with session_scope(services.sessions) as session:
        context = await resolve_run_token(session, presented)
        if context is None:
            _log_call(
                run_id=None,
                provider=provider,
                path=normalised,
                model=None,
                status=401,
                duration_ms=(time.perf_counter() - start) * 1000,
                upstream_request_id=None,
                streamed=False,
            )
            return _provider_error(
                provider, 401, "authentication_error", "the run token is not valid"
            )
        if context.provider != provider:
            _log_call(
                run_id=context.run_id,
                provider=provider,
                path=normalised,
                model=None,
                status=401,
                duration_ms=(time.perf_counter() - start) * 1000,
                upstream_request_id=None,
                streamed=False,
            )
            return _provider_error(
                provider,
                401,
                "authentication_error",
                "the run token is not valid for this provider",
            )
        if request.method != "POST":
            _log_call(
                run_id=context.run_id,
                provider=provider,
                path=normalised,
                model=None,
                status=405,
                duration_ms=(time.perf_counter() - start) * 1000,
                upstream_request_id=None,
                streamed=False,
            )
            return _provider_error(provider, 405, "invalid_request_error", "only POST is supported")
        if normalised not in _allowlist_for(provider):
            _log_call(
                run_id=context.run_id,
                provider=provider,
                path=normalised,
                model=None,
                status=404,
                duration_ms=(time.perf_counter() - start) * 1000,
                upstream_request_id=None,
                streamed=False,
            )
            return _provider_error(
                provider, 404, "not_found_error", "the requested endpoint is not supported"
            )

        raw, _ = await _read_body(request)
        if raw is None:
            _log_call(
                run_id=context.run_id,
                provider=provider,
                path=normalised,
                model=None,
                status=413,
                duration_ms=(time.perf_counter() - start) * 1000,
                upstream_request_id=None,
                streamed=False,
            )
            return _provider_error(
                provider, 413, "invalid_request_error", "request body is too large"
            )

        payload, _ = _parse_request_json(raw)
        if payload is None:
            _log_call(
                run_id=context.run_id,
                provider=provider,
                path=normalised,
                model=None,
                status=400,
                duration_ms=(time.perf_counter() - start) * 1000,
                upstream_request_id=None,
                streamed=False,
            )
            return _provider_error(
                provider, 400, "invalid_request_error", "request body is not valid JSON"
            )

        requested_model, _ = _validate_model(payload, services.prices)
        if requested_model is None:
            _log_call(
                run_id=context.run_id,
                provider=provider,
                path=normalised,
                model=None,
                status=400,
                duration_ms=(time.perf_counter() - start) * 1000,
                upstream_request_id=None,
                streamed=False,
            )
            return _provider_error(
                provider, 400, "invalid_request_error", "the request model is not configured"
            )

        streamed = payload.get("stream") is True
        body_changed = False
        if provider == "deepseek" and parser == "openai" and streamed:
            stream_options = payload.setdefault("stream_options", {})
            if isinstance(stream_options, dict):
                stream_options.setdefault("include_usage", True)
                body_changed = True
            else:
                payload["stream_options"] = {"include_usage": True}
                body_changed = True

        run = await ScopedRepo(session, Run, context.workspace_id).get(context.run_id)
        if run is None:
            _log_call(
                run_id=context.run_id,
                provider=provider,
                path=normalised,
                model=requested_model,
                status=401,
                duration_ms=(time.perf_counter() - start) * 1000,
                upstream_request_id=None,
                streamed=streamed,
            )
            return _provider_error(
                provider, 401, "authentication_error", "the run token is not valid"
            )

        refusal: BudgetExceeded | None = None
        try:
            await ensure_budget(session, run, services.settings, now=_now())
        except BudgetExceeded as exc:
            refusal = exc

        content = json.dumps(payload).encode("utf-8") if body_changed else raw

    if refusal is not None:
        await _publish_budget_event(services, context, refusal)
        _log_call(
            run_id=context.run_id,
            provider=provider,
            path=normalised,
            model=requested_model,
            status=403,
            duration_ms=(time.perf_counter() - start) * 1000,
            upstream_request_id=None,
            streamed=streamed,
        )
        return _budget_exceeded_response(provider, refusal)

    try:
        secret = await run_in_threadpool(services.store.require, _secret_name(provider))
    except MissingSecretError:
        _log_call(
            run_id=context.run_id,
            provider=provider,
            path=normalised,
            model=requested_model,
            status=503,
            duration_ms=(time.perf_counter() - start) * 1000,
            upstream_request_id=None,
            streamed=streamed,
        )
        return _provider_error(
            provider, 503, "api_error", "provider not configured", code="provider_not_configured"
        )

    base_url = (
        services.settings.anthropic_base_url
        if provider == "anthropic"
        else services.settings.junior_base_url
    )
    url = f"{base_url.rstrip('/')}/{normalised}"
    upstream_request = services.llm_http.build_request(
        "POST", url, content=content, headers=_upstream_headers(request, provider, secret)
    )
    try:
        upstream = await services.llm_http.send(upstream_request, stream=True)
    except httpx.TransportError as exc:
        _log_call(
            run_id=context.run_id,
            provider=provider,
            path=normalised,
            model=requested_model,
            status=502,
            duration_ms=(time.perf_counter() - start) * 1000,
            upstream_request_id=None,
            streamed=streamed,
        )
        return _provider_error(
            provider,
            502,
            "api_error",
            f"upstream {type(exc).__name__}",
            code="upstream_error",
        )

    request_id = _upstream_request_id(upstream.headers)
    _log_call(
        run_id=context.run_id,
        provider=provider,
        path=normalised,
        model=requested_model,
        status=upstream.status_code,
        duration_ms=(time.perf_counter() - start) * 1000,
        upstream_request_id=request_id,
        streamed=streamed,
    )

    meter = normalised != "v1/messages/count_tokens"
    accumulator = (
        UsageAccumulator(provider=provider, parser=parser, stream=streamed) if meter else None
    )
    return _relay_response(
        upstream,
        accumulator,
        context=context,
        services=services,
        requested_model=requested_model,
        provider=provider,
        path=normalised,
        request_id=request_id,
        meter=meter,
        request_bytes=len(content),
    )


@router.api_route(
    "/anthropic/{path:path}",
    methods=["POST", "GET", "PUT", "PATCH", "DELETE"],
    include_in_schema=False,
)
async def anthropic_gateway(request: Request, path: str) -> Response:
    """Forward an Anthropic-shaped request."""
    return await _handle(request, "anthropic", path)


@router.api_route(
    "/deepseek/{path:path}",
    methods=["POST", "GET", "PUT", "PATCH", "DELETE"],
    include_in_schema=False,
)
async def deepseek_gateway(request: Request, path: str) -> Response:
    """Forward a DeepSeek/OpenAI-shaped request."""
    return await _handle(request, "deepseek", path)
