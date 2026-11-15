"""JSON log file tests for doc 18b."""

from __future__ import annotations

import io
import json
import logging
import os
import stat
from collections.abc import Iterator
from pathlib import Path

import pytest
import structlog

from gaia.config.logging import LOG_DIR_MODE, LOG_FILE_MODE, _reset_for_tests, configure_logging
from gaia.security.redaction import REDACTED, register_secret

DUMMY_SECRET = "dummy" + "-file-secret"
GAIA_RUN_TOKEN = "gaia_rt_" + "a" * 24


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


def _read_jsonl(path: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in path.read_text().splitlines() if line]


def test_structlog_and_stdlib_lines_are_json_redacted_and_once(
    tmp_path: Path, output: io.StringIO
) -> None:
    log_dir = tmp_path / "logs"
    configure_logging(stream=output, log_dir=log_dir)
    register_secret(DUMMY_SECRET)

    structlog.get_logger().info("structlog event", secret=DUMMY_SECRET)
    logging.getLogger("httpx").info("stdlib event %s", DUMMY_SECRET)

    log_file = log_dir / "gaia.jsonl"
    events = _read_jsonl(log_file)
    assert len(events) == 2
    assert [event["event"] for event in events] == [
        "structlog event",
        "stdlib event [REDACTED]",
    ]
    assert events[0]["secret"] == REDACTED
    assert events[1]["level"] == "info"
    assert DUMMY_SECRET not in log_file.read_text()
    assert DUMMY_SECRET not in output.getvalue()
    assert REDACTED in output.getvalue()


def test_a_gaia_run_token_is_masked_in_both_destinations(
    tmp_path: Path, output: io.StringIO
) -> None:
    log_dir = tmp_path / "logs"
    configure_logging(stream=output, log_dir=log_dir)

    logging.getLogger("httpx").info("gateway token %s", GAIA_RUN_TOKEN)

    log_file = log_dir / "gaia.jsonl"
    assert GAIA_RUN_TOKEN not in log_file.read_text()
    assert GAIA_RUN_TOKEN not in output.getvalue()
    assert REDACTED in log_file.read_text()
    assert REDACTED in output.getvalue()


def test_rotation_produces_a_backup_file(tmp_path: Path, output: io.StringIO) -> None:
    log_dir = tmp_path / "logs"
    configure_logging(stream=output, log_dir=log_dir, max_bytes=100, backups=1)
    logger = structlog.get_logger()

    for index in range(5):
        logger.info("rotation", index=index)

    assert (log_dir / "gaia.jsonl.1").is_file()
    assert (log_dir / "gaia.jsonl").is_file()


def test_log_directory_and_file_are_owner_only(tmp_path: Path, output: io.StringIO) -> None:
    log_dir = tmp_path / "logs"
    configure_logging(stream=output, log_dir=log_dir)

    structlog.get_logger().info("mode check")

    log_file = log_dir / "gaia.jsonl"
    assert stat.S_IMODE(os.stat(log_dir).st_mode) == LOG_DIR_MODE
    assert stat.S_IMODE(os.stat(log_file).st_mode) == LOG_FILE_MODE


def test_a_console_only_reconfigure_keeps_the_file_handler(
    tmp_path: Path, output: io.StringIO
) -> None:
    log_dir = tmp_path / "logs"
    configure_logging(stream=output, log_dir=log_dir)
    structlog.get_logger().info("before lifespan")

    # `create_app`'s lifespan calls configure_logging() with no log_dir; it must
    # not undo the file handler `gaia serve` installed.
    configure_logging(stream=output)
    structlog.get_logger().info("after lifespan")

    events = _read_jsonl(log_dir / "gaia.jsonl")
    assert [event["event"] for event in events] == ["before lifespan", "after lifespan"]
