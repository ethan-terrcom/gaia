"""The error envelope, its handlers, and the request id every error carries.

Owned by doc 04 (task 4). Two rules:

- **One shape.** Every failure - a rejected token, a 404, a validation error, a
  crash - leaves as ``{"error": {"code", "message", "request_id"}}``. The UI
  parses one thing, and a client can quote a request id that matches a log line.
- **No internals.** A response never carries a traceback, a SQL statement or a
  secret. The traceback goes to the log (masked by doc 02's redaction), and the
  caller gets a sentence.

The request id is generated here rather than in the middleware because the
last-resort handler needs one even for a failure that happened before the
middleware could set it.
"""

from __future__ import annotations

import re
import uuid
from enum import StrEnum
from typing import Final, cast

import structlog
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel
from starlette.exceptions import HTTPException as StarletteHTTPException

from gaia.api.auth import AuthenticationError
from gaia.security.redaction import redact

logger = structlog.get_logger(__name__)

#: What an acceptable request id looks like. An inbound ``X-Request-ID`` is only
#: echoed (and logged) when it matches: the header is attacker-controlled input,
#: and a newline or a 4 KB string must not reach a log line.
REQUEST_ID_PATTERN: Final = re.compile(r"[A-Za-z0-9._-]{1,64}")

#: Response header carrying the request id.
REQUEST_ID_HEADER: Final = "X-Request-ID"


def new_request_id() -> str:
    """Return a fresh request id (32 hex characters)."""
    return uuid.uuid4().hex


class ErrorCode(StrEnum):
    """The machine-readable half of the envelope. Clients branch on this."""

    BAD_REQUEST = "bad_request"
    UNAUTHORIZED = "unauthorized"
    FORBIDDEN = "forbidden"
    NOT_FOUND = "not_found"
    METHOD_NOT_ALLOWED = "method_not_allowed"
    CONFLICT = "conflict"
    VALIDATION_ERROR = "validation_error"
    INTERNAL_ERROR = "internal_error"
    HTTP_ERROR = "http_error"


_CODES_BY_STATUS: Final[dict[int, ErrorCode]] = {
    400: ErrorCode.BAD_REQUEST,
    401: ErrorCode.UNAUTHORIZED,
    403: ErrorCode.FORBIDDEN,
    404: ErrorCode.NOT_FOUND,
    405: ErrorCode.METHOD_NOT_ALLOWED,
    409: ErrorCode.CONFLICT,
    422: ErrorCode.VALIDATION_ERROR,
}

#: Shown instead of an exception's own text when the status is not one we name.
GENERIC_MESSAGE: Final = "the request could not be completed"

#: Shown for anything that escaped the app, whatever it was.
INTERNAL_MESSAGE: Final = "the server hit an unexpected error"


class ErrorDetail(BaseModel):
    """The body of ``error`` in the envelope."""

    code: str
    message: str
    request_id: str


class ErrorEnvelope(BaseModel):
    """The only error body Gaia returns; doc 16 parses exactly this."""

    error: ErrorDetail


def request_id_of(request: Request) -> str:
    """Return the request id the middleware bound, or make one if it did not.

    ``getattr`` rather than plain attribute access: ``request.state.request_id``
    raises ``AttributeError`` when no middleware ran, which is exactly the case
    this fallback exists for.
    """
    existing: str | None = getattr(request.state, "request_id", None)
    return existing or new_request_id()


def error_response(
    request: Request,
    status_code: int,
    code: ErrorCode,
    message: str,
    *,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    """Build the envelope for ``status_code``.

    The message is redacted: it can come from an exception raised deep inside a
    provider client, which is exactly where an API key tends to appear.
    """
    envelope = ErrorEnvelope(
        error=ErrorDetail(
            code=code.value,
            message=redact(message),
            request_id=request_id_of(request),
        )
    )
    return JSONResponse(envelope.model_dump(), status_code=status_code, headers=headers)


async def _handle_http_error(request: Request, exc: Exception) -> Response:
    """Answer an ``HTTPException`` - ours or Starlette's - in the envelope.

    The cast is safe: handlers are registered per class (see
    :func:`install_error_handlers`), and the signature has to accept ``Exception``
    because that is what Starlette's ``ExceptionHandler`` type declares.
    """
    error = cast(StarletteHTTPException, exc)
    # Starlette fills an empty detail from its status table, so this is a string in
    # practice. The `isinstance` keeps a structured `detail={...}` from reaching
    # `redact()`, which only takes text.
    detail = error.detail if isinstance(error.detail, str) else ""
    return error_response(
        request,
        error.status_code,
        _CODES_BY_STATUS.get(error.status_code, ErrorCode.HTTP_ERROR),
        detail or GENERIC_MESSAGE,
        # Keeps 405's `Allow` and 401's `WWW-Authenticate` intact.
        headers=dict(error.headers or {}),
    )


async def _handle_validation_error(request: Request, exc: Exception) -> Response:
    """Answer a request-validation failure with the field, never the value.

    FastAPI's ``exc.errors()`` entries also carry the submitted ``input`` and a
    ``ctx``; neither is echoed, because a request body can hold a token.
    """
    errors = cast(RequestValidationError, exc).errors()
    first = errors[0] if errors else {}
    location = ".".join(str(part) for part in first.get("loc", ())) or "request"
    message = f"{location}: {first.get('msg', 'invalid value')}"
    return error_response(request, 422, ErrorCode.VALIDATION_ERROR, message)


async def _handle_authentication_error(request: Request, exc: Exception) -> Response:
    """Answer a failed token check with 401 and a ``Bearer`` challenge.

    ``WWW-Authenticate`` is what tells a client how to retry. The reasons stay
    distinct ("required" vs "not valid") because the only caller is the owner on
    their own machine, and neither ever contains the presented or the stored
    value.
    """
    error = cast(AuthenticationError, exc)
    return error_response(
        request,
        401,
        ErrorCode.UNAUTHORIZED,
        str(error) or "a bearer token is required",
        headers={"WWW-Authenticate": "Bearer"},
    )


async def _handle_unexpected_error(request: Request, exc: Exception) -> Response:
    """Answer a crash with 500 and log it, redacted, on the server side.

    Starlette re-raises after the handler returns, which is why a test that
    asserts on this body has to build its transport with
    ``httpx.ASGITransport(app=app, raise_app_exceptions=False)``.
    """
    logger.error(
        "api.unhandled_error",
        path=request.url.path,
        method=request.method,
        exc_info=exc,
    )
    return error_response(request, 500, ErrorCode.INTERNAL_ERROR, INTERNAL_MESSAGE)


def install_error_handlers(app: FastAPI) -> None:
    """Register every handler, so no path answers in a shape a client cannot parse."""
    app.add_exception_handler(StarletteHTTPException, _handle_http_error)
    app.add_exception_handler(RequestValidationError, _handle_validation_error)
    app.add_exception_handler(AuthenticationError, _handle_authentication_error)
    # `Exception` is the one key Starlette hands to ServerErrorMiddleware, which
    # is what turns a crash into this envelope instead of "Internal Server Error".
    app.add_exception_handler(Exception, _handle_unexpected_error)
