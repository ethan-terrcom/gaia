"""Doc 05 task 1: the process runner.

These tests spawn real processes, because the behaviour worth testing is what the
kernel does: a process group that dies together, a pipe that drains, an exit
status that survives, an environment that does not leak.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shlex
import signal
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from gaia.services.proc import (
    ENV_FALLBACKS,
    FORCED_ENV_VARS,
    KILL_GRACE_S,
    ProcError,
    _close_stream_reader,
    _kill_process_group,
    base_environment,
    run_proc,
)
from tests.git_repo import resolved

#: The interpreter running the tests: always present, whatever ``PATH`` says.
PYTHON = sys.executable


def python_source(source: str) -> list[str]:
    """An argv that runs ``source`` with this interpreter."""
    return [PYTHON, "-c", source]


async def test_run_proc_captures_stdout_and_status(tmp_path: Path) -> None:
    result = await run_proc(python_source("print('hello')"), cwd=tmp_path)

    assert result.ok
    assert result.returncode == 0
    assert result.stdout == "hello\n"
    assert result.stderr == ""
    assert not result.timed_out
    assert not result.truncated
    assert result.argv[0] == PYTHON
    assert result.cwd == resolved(tmp_path)
    assert result.pid > 0
    assert result.duration_s >= 0


async def test_run_proc_returns_a_failed_status_instead_of_raising(tmp_path: Path) -> None:
    """A non-zero exit is data: ``git merge-base`` uses exit 1 as an answer."""
    result = await run_proc(
        python_source("import sys; print('out'); print('bad', file=sys.stderr); sys.exit(3)"),
        cwd=tmp_path,
    )

    assert not result.ok
    assert result.returncode == 3
    assert result.stdout == "out\n"
    assert result.stderr == "bad\n"
    assert result.describe() == f"{PYTHON} exited with status 3"


async def test_run_proc_passes_argv_verbatim_without_a_shell(tmp_path: Path) -> None:
    """Metacharacters arrive as data: ``;``, ``$(…)`` and spaces are not special."""
    awkward = ["a b", "$(echo pwned)", "`id`", "semi;colon", "quote'\"", "*"]

    result = await run_proc(
        python_source("import json, sys; print(json.dumps(sys.argv[1:]))") + awkward,
        cwd=tmp_path,
    )

    assert json.loads(result.stdout) == awkward


async def test_run_proc_does_not_inherit_the_parent_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The doc's test: ``ANTHROPIC_API_KEY`` never reaches the child."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-not-a-real-key-000000")

    result = await run_proc(
        python_source("import json, os; print(json.dumps(dict(os.environ)))"), cwd=tmp_path
    )
    child_env = json.loads(result.stdout)

    assert "ANTHROPIC_API_KEY" not in child_env
    # Exactly the allowlist, nothing inherited. macOS injects
    # __CF_USER_TEXT_ENCODING at exec time, which no environment can prevent.
    injected_by_macos = {"__CF_USER_TEXT_ENCODING"}
    assert set(child_env) - injected_by_macos == {"PATH", "HOME", "LANG", *FORCED_ENV_VARS}
    assert child_env["GIT_TERMINAL_PROMPT"] == "0"


async def test_run_proc_merges_the_callers_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """What the caller passes is the only addition; other parent vars never leak."""
    monkeypatch.setenv("PARENT_ONLY", "leaked")

    result = await run_proc(
        python_source("import json, os; print(json.dumps(dict(os.environ)))"),
        cwd=tmp_path,
        env={"GAIA_EXTRA": "yes", "PATH": "/nowhere"},
    )
    child_env = json.loads(result.stdout)

    assert child_env["GAIA_EXTRA"] == "yes"
    assert child_env["PATH"] == "/nowhere"  # the caller wins over the parent
    assert "PARENT_ONLY" not in child_env


async def test_run_proc_truncates_each_stream_and_says_so(tmp_path: Path) -> None:
    result = await run_proc(
        python_source("import sys; sys.stdout.write('o' * 5000); sys.stderr.write('e' * 5000)"),
        cwd=tmp_path,
        max_output_bytes=100,
    )

    assert result.stdout == "o" * 100
    assert result.stderr == "e" * 100
    assert result.stdout_truncated
    assert result.stderr_truncated
    assert result.truncated


async def test_run_proc_drains_output_past_the_cap(tmp_path: Path) -> None:
    """A child writing far more than the cap still finishes: the pipe drains."""
    result = await run_proc(
        python_source("import sys; sys.stdout.write('x' * 2_000_000); print('done')"),
        cwd=tmp_path,
        max_output_bytes=1024,
    )

    assert result.ok
    assert result.stdout_truncated
    assert len(result.stdout) == 1024


async def test_run_proc_times_out_and_leaves_no_zombie(tmp_path: Path) -> None:
    """The doc's test: a ``sleep`` past the timeout dies, and is reaped."""
    started = time.monotonic()
    result = await run_proc(["/bin/sleep", "10"], cwd=tmp_path, timeout_s=0.2)
    elapsed = time.monotonic() - started

    assert result.timed_out
    assert result.returncode == -9  # SIGKILL
    assert elapsed < 5
    assert result.describe() == "/bin/sleep timed out after 0.2s and was killed"
    # Reaped, not a zombie: the pid no longer exists at all. A zombie would still
    # answer signal 0, because its process table entry survives until a wait().
    with pytest.raises(ProcessLookupError):
        os.kill(result.pid, 0)


async def test_run_proc_kills_the_whole_process_group(tmp_path: Path) -> None:
    """A grandchild in the same group dies with the child, or it would leak."""
    pid_file = tmp_path / "grandchild.pid"
    script = f"sleep 30 & echo $! > {pid_file}; wait"

    result = await run_proc(["/bin/sh", "-c", script], cwd=tmp_path, timeout_s=0.5)
    assert result.timed_out
    assert pid_file.exists(), "the grandchild should have written its pid"
    grandchild = int(pid_file.read_text().strip())

    for _ in range(50):
        try:
            os.kill(grandchild, 0)
        except ProcessLookupError:
            break
        await asyncio.sleep(0.1)
    else:  # pragma: no cover - only on a real leak
        pytest.fail(f"grandchild {grandchild} survived the process-group kill")


async def test_an_escaped_grandchild_bounds_the_reap(tmp_path: Path) -> None:
    """F2: a grandchild that called ``setsid`` cannot hang the timeout path."""
    pid_file = tmp_path / "escaped.pid"
    source = (
        "import os,time; "
        f"open({str(pid_file)!r},'w').write(str(os.getpid())); "
        "os.setsid(); time.sleep(30)"
    )
    script = f"{PYTHON} -c {shlex.quote(source)} & echo started"

    started = time.monotonic()
    try:
        result = await run_proc(["/bin/sh", "-c", script], cwd=tmp_path, timeout_s=1)
        elapsed = time.monotonic() - started

        assert result.timed_out
        assert result.escaped
        assert "started" in result.stdout
        assert elapsed < 1 + KILL_GRACE_S + 2
    finally:
        # The escaped sleeper is no longer in the killed group by design, so the
        # test finds and kills it - even when an assertion above failed.
        _kill_pid_file(pid_file)


def _kill_pid_file(pid_file: Path) -> None:
    """SIGKILL the process whose pid ``pid_file`` holds, if it is still there."""
    if pid_file.exists() and pid_file.read_text().strip():
        with contextlib.suppress(ProcessLookupError):
            os.kill(int(pid_file.read_text().strip()), signal.SIGKILL)


async def test_cancelling_with_an_escaped_grandchild_is_bounded(tmp_path: Path) -> None:
    """F2 (lead review): the cancellation path is bounded like the timeout path."""
    pid_file = tmp_path / "escaped-cancel.pid"
    source = (
        "import os,time; "
        f"open({str(pid_file)!r},'w').write(str(os.getpid())); "
        "os.setsid(); time.sleep(30)"
    )
    script = f"{PYTHON} -c {shlex.quote(source)} & sleep 30"
    task = asyncio.create_task(run_proc(["/bin/sh", "-c", script], cwd=tmp_path, timeout_s=60))
    try:
        for _ in range(100):
            if pid_file.exists() and pid_file.read_text().strip():
                break
            await asyncio.sleep(0.05)

        started = time.monotonic()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert time.monotonic() - started < KILL_GRACE_S + 2
    finally:
        _kill_pid_file(pid_file)


async def test_cancelling_run_proc_kills_the_command(tmp_path: Path) -> None:
    """Doc 14 cancels runs: nothing may keep running behind a cancelled call."""
    pid_file = tmp_path / "cancelled.pid"
    task = asyncio.create_task(
        run_proc(["/bin/sh", "-c", f"sleep 30 & echo $! > {pid_file}; wait"], cwd=tmp_path)
    )
    for _ in range(100):
        if pid_file.exists() and pid_file.read_text().strip():
            break
        await asyncio.sleep(0.05)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    grandchild = int(pid_file.read_text().strip())
    for _ in range(50):
        try:
            os.kill(grandchild, 0)
        except ProcessLookupError:
            break
        await asyncio.sleep(0.1)
    else:  # pragma: no cover - only on a real leak
        pytest.fail(f"grandchild {grandchild} outlived the cancelled call")


async def test_run_proc_refuses_a_command_line_string(tmp_path: Path) -> None:
    """A string argv would mean shell splitting, which this module forbids.

    ``str`` satisfies ``Sequence[str]``, so the type checker cannot catch this one;
    the runtime check is the only thing between a caller and word splitting.
    """
    with pytest.raises(TypeError, match="never uses a shell"):
        await run_proc("echo hello", cwd=tmp_path)


@pytest.mark.parametrize(
    ("argv", "error", "match"),
    [
        ([], ProcError, "argv is empty"),
        (["echo", "a\x00b"], ProcError, "NUL byte"),
        (["echo", 3], TypeError, "must be str"),
    ],
)
async def test_run_proc_rejects_malformed_argv(
    tmp_path: Path, argv: list[object], error: type[Exception], match: str
) -> None:
    with pytest.raises(error, match=match):
        await run_proc(argv, cwd=tmp_path)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        ({"timeout_s": 0}, "timeout_s must be positive"),
        ({"timeout_s": -1.0}, "timeout_s must be positive"),
        ({"max_output_bytes": 0}, "max_output_bytes must be positive"),
    ],
)
async def test_run_proc_rejects_bad_limits(
    tmp_path: Path, kwargs: dict[str, Any], match: str
) -> None:
    with pytest.raises(ProcError, match=match):
        await run_proc([PYTHON, "--version"], cwd=tmp_path, **kwargs)


async def test_run_proc_rejects_a_missing_working_directory(tmp_path: Path) -> None:
    with pytest.raises(ProcError, match="is not a directory"):
        await run_proc([PYTHON, "--version"], cwd=tmp_path / "gone")


async def test_run_proc_reports_a_missing_binary(tmp_path: Path) -> None:
    with pytest.raises(ProcError, match="could not run"):
        await run_proc(["definitely-not-a-real-binary"], cwd=tmp_path)


def test_kill_process_group_ignores_a_process_that_is_already_gone() -> None:
    """A child that exits between the timeout and the signal is not an error."""
    _kill_process_group(_unused_pid())


def _unused_pid() -> int:
    """A pid that does not exist: the largest pid plus one, as Linux allows."""
    return 2**22 + 1


async def test_close_stream_reader_ignores_a_missing_transport() -> None:
    """A fresh ``StreamReader`` has no transport yet; closing it is a no-op."""
    _close_stream_reader(asyncio.StreamReader())


def test_base_environment_falls_back_when_the_parent_is_stripped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in ("PATH", "LANG"):
        monkeypatch.delenv(name, raising=False)

    env = base_environment()

    assert env["PATH"] == ENV_FALLBACKS["PATH"]
    assert env["LANG"] == ENV_FALLBACKS["LANG"]
    assert env["HOME"]  # from the parent, or from the password database
    assert env["GIT_TERMINAL_PROMPT"] == "0"
