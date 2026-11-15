"""Tests for the stdlib bridge and uvicorn query-string redaction (doc 18b).

``structlog.testing.capture_logs()`` only sees structlog's own event dicts, so
these tests configure real logging and render into a ``StringIO``.
"""

from __future__ import annotations

import io
import logging
from collections.abc import Iterator

import pytest

from gaia.config.logging import (
    _remove_handler,
    _reset_for_tests,
    _UvicornAccessQueryStringFilter,
    configure_logging,
)
from gaia.security.redaction import REDACTED, register_secret

DUMMY_SECRET = "dummy" + "-stdlib-value"
OPENAI_SHAPED = "sk-" + "a" * 24
UVICORN_MSG = '%s - "%s %s HTTP/%s" %d'


@pytest.fixture
def output() -> io.StringIO:
    return io.StringIO()


@pytest.fixture(autouse=True)
def restore_logging() -> Iterator[None]:
    """configure_logging() sets global state; do not leak it into other tests."""
    try:
        yield
    finally:
        _reset_for_tests()


def _uvicorn_record() -> logging.LogRecord:
    """Build a real access record the way uvicorn does."""
    return logging.LogRecord(
        name="uvicorn.access",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg=UVICORN_MSG,
        args=(
            "127.0.0.1:51234",
            "GET",
            "/api/v1/stream?ticket=ticket-value-123",
            "1.1",
            200,
        ),
        exc_info=None,
    )


def test_a_stdlib_log_line_is_redacted(output: io.StringIO) -> None:
    configure_logging(stream=output)
    register_secret(DUMMY_SECRET)

    logging.getLogger("httpx").info("provider said %s", DUMMY_SECRET)

    rendered = output.getvalue()
    assert REDACTED in rendered
    assert DUMMY_SECRET not in rendered


def test_a_stdlib_token_shape_is_redacted(output: io.StringIO) -> None:
    configure_logging(stream=output)

    logging.getLogger("httpx").info("Authorization: Bearer %s", OPENAI_SHAPED)

    rendered = output.getvalue()
    assert REDACTED in rendered
    assert OPENAI_SHAPED not in rendered


def test_uvicorn_access_query_string_is_dropped(output: io.StringIO) -> None:
    configure_logging(stream=output)

    logging.getLogger("uvicorn.access").handle(_uvicorn_record())

    rendered = output.getvalue()
    assert "?ticket=" not in rendered
    assert "ticket-value-123" not in rendered
    assert "GET /api/v1/stream HTTP/1.1" in rendered


def test_uvicorn_access_line_is_emitted_once(output: io.StringIO) -> None:
    configure_logging(stream=output)
    access_logger = logging.getLogger("uvicorn.access")

    # With `gaia serve` passing log_config=None, uvicorn installs no handler of
    # its own; the only handler is the root one below.
    assert access_logger.handlers == []
    access_logger.handle(_uvicorn_record())

    assert output.getvalue().count("GET /api/v1/stream HTTP/1.1") == 1


def test_uvicorn_filter_ignores_records_without_uvicorn_args() -> None:
    filter_ = _UvicornAccessQueryStringFilter()
    record = logging.LogRecord(
        name="httpx",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="plain message",
        args=None,
        exc_info=None,
    )

    assert filter_.filter(record) is True
    assert record.args is None


def test_remove_handler_ignores_a_handler_not_on_the_root_logger() -> None:
    _remove_handler(logging.Handler())


def test_uvicorn_filter_ignores_a_non_string_path() -> None:
    filter_ = _UvicornAccessQueryStringFilter()
    record = logging.LogRecord(
        name="uvicorn.access",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg=UVICORN_MSG,
        args=("127.0.0.1:51234", "GET", 123, "1.1", 200),
        exc_info=None,
    )

    assert filter_.filter(record) is True
    assert record.args == ("127.0.0.1:51234", "GET", 123, "1.1", 200)
