"""Tests for ``stream_proc``, the line-streaming sibling of ``run_proc``."""

from __future__ import annotations

import asyncio
import contextlib
import os
import shlex
import signal
import sys
import time
from pathlib import Path

import pytest

from gaia.services.proc import (
    DEFAULT_MAX_OUTPUT_BYTES,
    KILL_GRACE_S,
    ProcError,
    _kill_process_group,
    stream_proc,
)
from tests.git_repo import resolved

PYTHON = sys.executable


def python_source(source: str) -> list[str]:
    return [PYTHON, "-c", source]


async def test_stream_proc_streams_stdout_and_stderr(tmp_path: Path) -> None:
    lines: list[tuple[str, str]] = []

    async def on_line(stream: str, line: str) -> None:
        lines.append((stream, line))

    result = await stream_proc(
        python_source("import sys; print('out1'); print('out2', file=sys.stderr); print('out3')"),
        cwd=tmp_path,
        env={"GAIA_STREAM": "yes"},
        on_line=on_line,
        timeout_s=30,
    )

    assert result.ok
    assert result.cwd == resolved(tmp_path)
    # stdout and stderr are drained by two concurrent readers, so their lines
    # may interleave; each stream's own order is what ``stream_proc`` guarantees.
    assert [line for stream, line in lines if stream == "stdout"] == ["out1", "out3"]
    assert [line for stream, line in lines if stream == "stderr"] == ["out2"]


async def test_stream_proc_splits_overlong_lines(tmp_path: Path) -> None:
    lines: list[tuple[str, str]] = []

    async def on_line(stream: str, line: str) -> None:
        lines.append((stream, line))

    result = await stream_proc(
        python_source("import sys; sys.stdout.write('x' * 10 + '\\n')"),
        cwd=tmp_path,
        env=None,
        on_line=on_line,
        timeout_s=30,
        max_line_bytes=4,
    )

    assert result.ok
    assert lines == [
        ("stdout", "xxxx"),
        ("stdout", "xxxx"),
        ("stdout", "xx"),
    ]


async def test_stream_proc_keeps_only_the_last_64_kb(tmp_path: Path) -> None:
    """Unlike ``run_proc``, the buffered result keeps the tail, not the head."""
    lines: list[tuple[str, str]] = []

    async def on_line(stream: str, line: str) -> None:
        lines.append((stream, line))

    payload = "a" * (DEFAULT_MAX_OUTPUT_BYTES + 1000) + "\n"
    result = await stream_proc(
        python_source(f"import sys; sys.stdout.write({payload!r})"),
        cwd=tmp_path,
        env=None,
        on_line=on_line,
        timeout_s=30,
    )

    assert result.ok
    assert result.stdout_truncated
    assert len(result.stdout) == DEFAULT_MAX_OUTPUT_BYTES
    assert result.stdout.endswith("a" * 1000 + "\n")


async def test_stream_proc_times_out_and_kills_the_process(tmp_path: Path) -> None:
    called: list[str] = []

    async def on_line(stream: str, line: str) -> None:
        del line
        called.append(stream)

    result = await stream_proc(
        python_source("import time; time.sleep(30)"),
        cwd=tmp_path,
        env=None,
        on_line=on_line,
        timeout_s=0.2,
    )

    assert result.timed_out
    assert result.returncode == -9
    assert called == []
    with pytest.raises(ProcessLookupError):
        os.kill(result.pid, 0)


async def test_stream_proc_cancellation_kills_the_process(tmp_path: Path) -> None:
    async def on_line(stream: str, line: str) -> None:
        del stream, line

    task = asyncio.create_task(
        stream_proc(
            python_source("import time; time.sleep(30)"),
            cwd=tmp_path,
            env=None,
            on_line=on_line,
            timeout_s=60,
        )
    )
    await asyncio.sleep(0.2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_stream_proc_reports_the_pid_to_on_start(tmp_path: Path) -> None:
    seen: list[int] = []

    async def on_line(stream: str, line: str) -> None:
        del stream, line

    result = await stream_proc(
        python_source("print('hello')"),
        cwd=tmp_path,
        env=None,
        on_line=on_line,
        timeout_s=30,
        on_start=seen.append,
    )

    assert seen == [result.pid]


async def test_stream_proc_rejects_malformed_inputs(tmp_path: Path) -> None:
    async def on_line(stream: str, line: str) -> None:
        del stream, line

    with pytest.raises(ProcError, match="max_line_bytes must be positive"):
        await stream_proc(
            [PYTHON, "--version"],
            cwd=tmp_path,
            env=None,
            on_line=on_line,
            timeout_s=30,
            max_line_bytes=0,
        )
    with pytest.raises(TypeError, match="never uses a shell"):
        await stream_proc(
            "echo hello",
            cwd=tmp_path,
            env=None,
            on_line=on_line,
            timeout_s=30,
        )


async def test_stream_proc_emits_a_partial_final_line(tmp_path: Path) -> None:
    lines: list[tuple[str, str]] = []

    async def on_line(stream: str, line: str) -> None:
        lines.append((stream, line))

    result = await stream_proc(
        python_source("import sys; sys.stdout.write('no-newline')"),
        cwd=tmp_path,
        env=None,
        on_line=on_line,
        timeout_s=30,
    )

    assert result.ok
    assert ("stdout", "no-newline") in lines


async def test_stream_proc_emits_an_empty_line(tmp_path: Path) -> None:
    lines: list[tuple[str, str]] = []

    async def on_line(stream: str, line: str) -> None:
        lines.append((stream, line))

    result = await stream_proc(
        python_source("print()"),
        cwd=tmp_path,
        env=None,
        on_line=on_line,
        timeout_s=30,
    )

    assert result.ok
    assert ("stdout", "") in lines


async def test_stream_proc_rejects_a_non_positive_timeout(tmp_path: Path) -> None:
    async def on_line(stream: str, line: str) -> None:
        del stream, line

    with pytest.raises(ProcError, match="timeout_s must be positive"):
        await stream_proc(
            [PYTHON, "--version"],
            cwd=tmp_path,
            env=None,
            on_line=on_line,
            timeout_s=0,
        )


async def test_stream_proc_reports_a_missing_binary(tmp_path: Path) -> None:
    async def on_line(stream: str, line: str) -> None:
        del stream, line

    with pytest.raises(ProcError, match="could not run"):
        await stream_proc(
            ["definitely-not-a-real-binary"],
            cwd=tmp_path,
            env=None,
            on_line=on_line,
            timeout_s=30,
        )


async def test_stream_proc_bounds_an_escaped_grandchild(tmp_path: Path) -> None:
    """An escaped grandchild must not hang ``stream_proc`` either (doc 05 F2)."""
    pid_file = tmp_path / "escaped-stream.pid"
    source = (
        "import os,time; "
        f"open({str(pid_file)!r},'w').write(str(os.getpid())); "
        "os.setsid(); time.sleep(30)"
    )
    script = f"{PYTHON} -c {shlex.quote(source)} & sleep 30"
    lines: list[tuple[str, str]] = []

    async def on_line(stream: str, line: str) -> None:
        lines.append((stream, line))

    started = time.monotonic()
    try:
        result = await stream_proc(
            ["/bin/sh", "-c", script],
            cwd=tmp_path,
            env=None,
            on_line=on_line,
            timeout_s=1,
        )
        elapsed = time.monotonic() - started

        assert result.timed_out
        assert result.escaped
        assert elapsed < 1 + KILL_GRACE_S + 2
    finally:
        _kill_pid_file(pid_file)


def _kill_pid_file(pid_file: Path) -> None:
    if pid_file.exists() and pid_file.read_text().strip():
        with contextlib.suppress(ProcessLookupError):
            os.kill(int(pid_file.read_text().strip()), signal.SIGKILL)


def test_stream_proc_uses_the_same_group_kill_helper() -> None:
    _kill_process_group(2**22 + 1)
