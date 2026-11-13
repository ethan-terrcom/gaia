"""Tests for ``gaia.config.logging`` (doc 02, task 4).

``structlog.testing.capture_logs()`` replaces the processor chain, so it cannot
prove that redaction runs. These tests configure real logging and render into a
``StringIO`` instead.
"""

from __future__ import annotations

import io
import logging
from collections.abc import Iterator

import pytest
import structlog

from gaia.config.logging import configure_logging
from gaia.security.redaction import REDACTED, register_secret

DUMMY_SECRET = "dummy" + "-logged-value"
OPENAI_SHAPED = "sk-" + "a" * 24


@pytest.fixture
def output() -> io.StringIO:
    return io.StringIO()


@pytest.fixture(autouse=True)
def restore_structlog() -> Iterator[None]:
    """configure_logging() sets global state; do not leak it into other tests."""
    try:
        yield
    finally:
        structlog.reset_defaults()


def test_a_logged_secret_is_redacted(output: io.StringIO) -> None:
    configure_logging(stream=output)
    register_secret(DUMMY_SECRET)

    structlog.get_logger().info("storing a credential", value=DUMMY_SECRET)

    rendered = output.getvalue()
    assert REDACTED in rendered
    assert DUMMY_SECRET not in rendered


def test_an_unregistered_token_shape_is_redacted(output: io.StringIO) -> None:
    configure_logging(stream=output)

    structlog.get_logger().info("request sent", authorization=OPENAI_SHAPED)

    rendered = output.getvalue()
    assert REDACTED in rendered
    assert OPENAI_SHAPED not in rendered


def test_traceback_text_is_redacted(output: io.StringIO) -> None:
    configure_logging(stream=output)
    register_secret(DUMMY_SECRET)
    logger = structlog.get_logger()

    try:
        raise RuntimeError(f"provider rejected {DUMMY_SECRET}")
    except RuntimeError:
        logger.error("llm call failed", exc_info=True)

    rendered = output.getvalue()
    # The shape of the traceback and the message survive; only the secret goes.
    assert "Traceback (most recent call last)" in rendered
    assert "llm call failed" in rendered
    assert DUMMY_SECRET not in rendered
    assert REDACTED in rendered


def test_a_value_nested_in_a_dict_is_redacted(output: io.StringIO) -> None:
    configure_logging(stream=output)
    register_secret(DUMMY_SECRET)

    structlog.get_logger().info("event", payload={"headers": [DUMMY_SECRET]})

    assert DUMMY_SECRET not in output.getvalue()


def test_the_level_is_honoured(output: io.StringIO) -> None:
    configure_logging(level=logging.WARNING, stream=output)

    structlog.get_logger().info("too quiet to print")
    structlog.get_logger().warning("loud enough")

    rendered = output.getvalue()
    assert "too quiet to print" not in rendered
    assert "loud enough" in rendered
    assert "[warning" in rendered
