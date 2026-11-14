"""The one module in Gaia that may spawn a process (doc 05 task 1).

Every layer above this file - git, the tool registry of doc 09b, the runtimes of
doc 10 - calls :func:`run_proc` and nothing else. Ruff enforces that: importing
``subprocess``, ``os.system`` or ``asyncio.create_subprocess_exec`` anywhere else
is a lint error, and this file is the single exception
(``[tool.ruff.lint.per-file-ignores]``).

Five decisions make a spawned command survivable:

- **No shell, ever.** ``argv`` is passed to ``create_subprocess_exec``, so a
  ticket title, a filename or a model's suggestion is an argument and never a
  command. An empty list is a bug, not "run nothing".
- **A minimal environment.** The child gets ``PATH``, ``HOME``, ``LANG`` and
  ``GIT_TERMINAL_PROMPT=0`` from the parent, plus whatever the caller passes
  explicitly. It does **not** inherit the rest, so ``ANTHROPIC_API_KEY`` and
  every other secret in Gaia's environment stay in Gaia (ARCHITECTURE 6.1).
- **Its own process group** (``start_new_session=True``) so that a timeout can
  kill the whole tree with one ``killpg``. Killing the direct child is not
  enough: ``sh -c 'pytest | tee log'`` leaves the work behind.
- **A timeout that is enforced.** On expiry the group gets ``SIGKILL`` and the
  child is waited for, so no zombie is left behind (the doc's test).
- **Bounded output.** A test run that prints 40 MB must not become a 40 MB
  string. Each stream is drained in fixed chunks, only the first
  ``max_output_bytes`` are kept, and the result says whether anything was
  dropped - the rest is read and thrown away rather than left to block the
  child on a full pipe.

TODO(gaia-10): the runtimes need ``stream_proc``, which yields output as it
arrives. It belongs next to :func:`run_proc` and must keep this module the only
one that spawns; nothing here streams on purpose, because every caller from doc
05 to doc 15a wants a buffered result.
"""

from __future__ import annotations

import asyncio
import os
import signal
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import cast

import structlog

logger = structlog.get_logger(__name__)

#: Seconds a command may run before its process group is killed.
DEFAULT_TIMEOUT_S = 30.0

#: Bytes kept from each stream. Doc 09a's ``run_command`` wants the same 64 KB.
DEFAULT_MAX_OUTPUT_BYTES = 64 * 1024

#: How much is read from a pipe at once. Memory hygiene, not a size limit: a
#: child that writes megabytes is drained chunk by chunk either way.
READ_CHUNK_BYTES = 32 * 1024

#: Variables inherited from the parent when it sets them. A scrubbed ``HOME``
#: (doc 09a) is the caller's decision, passed through ``env``.
INHERITED_ENV_VARS: tuple[str, ...] = ("PATH", "HOME", "LANG")

#: Set on every child, whatever the caller passes. A tool that waits for a
#: terminal password would hang the run until the timeout; git is the one that
#: matters today.
FORCED_ENV_VARS: dict[str, str] = {"GIT_TERMINAL_PROMPT": "0"}

#: Used when the parent does not set a value at all (a stripped environment).
ENV_FALLBACKS: dict[str, str] = {
    "PATH": "/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin",
    "LANG": "C.UTF-8",
}

#: Seconds to wait after ``SIGKILL`` for the leader and both pipe readers to
#: finish. A grandchild that called ``setsid()`` is no longer in the killed
#: group and can keep a pipe open forever, so the reap is bounded.
KILL_GRACE_S = 5.0

#: How much of the program name goes into a log line. Never the whole argv: a
#: caller will eventually put a token in an argument by mistake.
_MAX_LOGGED_ARG = 200


class ProcError(RuntimeError):
    """A process could not be started, or was asked for something impossible.

    A command that *ran* and failed is not an error here: the caller gets a
    :class:`ProcResult` with the exit code, because the exit code is data (``git
    merge-base`` exits 1 when there is no common ancestor, and that is the
    answer, not a failure).
    """


@dataclass(frozen=True)
class ProcResult:
    """What one command produced.

    ``pid`` is here for the timeout test, which proves the process is gone
    afterwards, and for log correlation.
    """

    argv: tuple[str, ...]
    cwd: Path
    pid: int
    returncode: int
    stdout: str
    stderr: str
    stdout_truncated: bool
    stderr_truncated: bool
    duration_s: float
    timed_out: bool
    escaped: bool = False

    @property
    def ok(self) -> bool:
        """True when the command finished on its own with status 0."""
        return self.returncode == 0 and not self.timed_out

    @property
    def truncated(self) -> bool:
        """True when either stream was cut short at the byte cap."""
        return self.stdout_truncated or self.stderr_truncated

    def describe(self) -> str:
        """A one-line summary for an error message: program, status, output."""
        if self.timed_out:
            return f"{self.argv[0]} timed out after {self.duration_s:.1f}s and was killed"
        return f"{self.argv[0]} exited with status {self.returncode}"


def base_environment() -> dict[str, str]:
    """The minimal environment a child gets before the caller's additions.

    A fresh dict built from :data:`INHERITED_ENV_VARS`: an allowlist rather than
    a denylist, because a denylist of secret names is one commit behind the
    provider SDKs.
    """
    env = {name: value for name in INHERITED_ENV_VARS if (value := os.environ.get(name))}
    for name, fallback in ENV_FALLBACKS.items():
        env.setdefault(name, fallback)
    env.setdefault("HOME", str(Path.home()))
    env.update(FORCED_ENV_VARS)
    return env


async def run_proc(
    argv: Sequence[str],
    cwd: Path,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    env: Mapping[str, str] | None = None,
    max_output_bytes: int = DEFAULT_MAX_OUTPUT_BYTES,
) -> ProcResult:
    """Run ``argv`` in ``cwd`` with no shell, and return what it produced.

    Raises :class:`ProcError` only when the command could not be started (a
    missing binary, a ``cwd`` that is not a directory, a nonsense argument). A
    command that fails, or that had to be killed, is a result with
    ``returncode != 0`` or ``timed_out=True``.
    """
    _check_argv(argv)
    if timeout_s <= 0:
        raise ProcError(f"timeout_s must be positive, not {timeout_s!r}")
    if max_output_bytes <= 0:
        raise ProcError(f"max_output_bytes must be positive, not {max_output_bytes!r}")

    directory = _existing_directory(cwd)

    child_env = base_environment()
    if env is not None:
        child_env.update(env)

    started = time.monotonic()
    try:
        process = await asyncio.create_subprocess_exec(
            *argv,
            cwd=str(directory),
            env=child_env,
            # A tool that reads stdin would wait for input no one will send.
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            # Its own group, so a timeout kills everything it started.
            start_new_session=True,
        )
    except (FileNotFoundError, NotADirectoryError, PermissionError) as exc:
        raise ProcError(f"could not run {argv[0]!r}: {exc}") from exc

    # PIPE was requested above, so both streams exist; a cast keeps mypy happy
    # without an `assert`, which bandit (S101) flags as a real assertion.
    stdout_stream = cast("asyncio.StreamReader", process.stdout)
    stderr_stream = cast("asyncio.StreamReader", process.stderr)
    stdout_task = asyncio.create_task(_read_capped(stdout_stream, max_output_bytes))
    stderr_task = asyncio.create_task(_read_capped(stderr_stream, max_output_bytes))

    timed_out = False
    escaped = False
    try:
        returncode = await asyncio.wait_for(process.wait(), timeout_s)
    except TimeoutError:
        timed_out = True
        _kill_process_group(process.pid)
        (
            returncode,
            (stdout, stdout_truncated),
            (stderr, stderr_truncated),
            escaped,
        ) = await _reap_after_kill(process, stdout_stream, stderr_stream, stdout_task, stderr_task)
    except BaseException:
        # The caller was cancelled (doc 14 cancels runs). A command must not
        # outlive the call that started it, and its two readers must not be left
        # pending, so kill, reap and re-raise.
        _kill_process_group(process.pid)
        # Bounded like the timeout path: an escaped grandchild must not hang
        # a cancelled run either (review F2, lead review of 05b).
        await _reap_after_kill(process, stdout_stream, stderr_stream, stdout_task, stderr_task)
        raise
    else:
        (stdout, stdout_truncated), (stderr, stderr_truncated) = await asyncio.gather(
            stdout_task, stderr_task
        )

    result = ProcResult(
        argv=tuple(argv),
        cwd=directory,
        pid=process.pid,
        returncode=returncode,
        stdout=stdout,
        stderr=stderr,
        stdout_truncated=stdout_truncated,
        stderr_truncated=stderr_truncated,
        duration_s=time.monotonic() - started,
        timed_out=timed_out,
        escaped=escaped,
    )
    if escaped:
        # Only the program and pid: a warning is a log line like any other, and
        # argv can hold a token.
        logger.warning("proc.escaped", program=argv[0][:_MAX_LOGGED_ARG], pid=process.pid)
    # Only the program and the argument count: an argv can hold a token.
    logger.debug(
        "proc.finished",
        program=argv[0][:_MAX_LOGGED_ARG],
        argc=len(argv),
        returncode=result.returncode,
        timed_out=result.timed_out,
        escaped=result.escaped,
        truncated=result.truncated,
        duration_s=round(result.duration_s, 3),
    )
    return result


def _existing_directory(cwd: Path) -> Path:
    """Resolve ``cwd`` and require it to be a directory.

    A plain function rather than inline code: ``Path.resolve()`` is a blocking
    syscall, and ruff's ASYNC240 keeps those out of coroutines. It costs
    microseconds here, but the rule is the rule.
    """
    directory = Path(cwd).resolve()
    if not directory.is_dir():
        raise ProcError(f"working directory {str(directory)!r} is not a directory")
    return directory


def _check_argv(argv: Sequence[str]) -> None:
    """Refuse anything that is not an argv list of plain strings.

    A ``str`` here would mean someone expected shell word splitting, which is
    exactly what this module exists to prevent.
    """
    if isinstance(argv, str | bytes):
        raise TypeError(
            "argv must be a sequence of arguments, not one string: run_proc never uses a shell"
        )
    if not argv:
        raise ProcError("argv is empty: a command needs at least a program")
    for argument in argv:
        if not isinstance(argument, str):
            raise TypeError(f"argv entries must be str, not {type(argument).__name__}")
        if "\x00" in argument:
            raise ProcError("an argv entry contains a NUL byte")


def _kill_process_group(pid: int) -> None:
    """``SIGKILL`` the group led by ``pid``, if it is still there.

    ``start_new_session`` made ``pid`` the group id, so this reaches every
    process the child started. ``SIGKILL`` rather than a polite ``SIGTERM``: a
    timed-out command has already been given its whole budget, and anything that
    catches ``SIGTERM`` to keep working would keep working.
    """
    try:
        os.killpg(pid, signal.SIGKILL)
    except ProcessLookupError:
        # It exited between the timeout and the signal; nothing to kill.
        pass


async def _reap_after_kill(
    process: asyncio.subprocess.Process,
    stdout_stream: asyncio.StreamReader,
    stderr_stream: asyncio.StreamReader,
    stdout_task: asyncio.Task[tuple[str, bool]],
    stderr_task: asyncio.Task[tuple[str, bool]],
) -> tuple[int, tuple[str, bool], tuple[str, bool], bool]:
    """Reap a killed leader and both readers, bounded by :data:`KILL_GRACE_S`.

    Returns ``(returncode, stdout, stderr, escaped)``. When the grace period
    runs out, a grandchild that left the process group is keeping a pipe open;
    the only safe move is to close our end of the pipes, cancel the readers and
    keep the output read so far.
    """
    try:
        returncode, (stdout, stdout_truncated), (stderr, stderr_truncated) = await asyncio.wait_for(
            asyncio.gather(process.wait(), stdout_task, stderr_task),
            timeout=KILL_GRACE_S,
        )
        return returncode, (stdout, stdout_truncated), (stderr, stderr_truncated), False
    except TimeoutError:
        # ``process.returncode`` is set as soon as the leader is reaped, even
        # while a grandchild still holds the pipes open.
        returncode = process.returncode if process.returncode is not None else -9
        _close_stream_reader(stdout_stream)
        _close_stream_reader(stderr_stream)
        stdout_task.cancel()
        stderr_task.cancel()
        (stdout, stdout_truncated), (stderr, stderr_truncated) = await asyncio.gather(
            stdout_task, stderr_task
        )
        return returncode, (stdout, stdout_truncated), (stderr, stderr_truncated), True


def _close_stream_reader(stream: asyncio.StreamReader) -> None:
    """Close the transport behind ``stream``.

    ``asyncio`` does not expose the transport on ``StreamReader``; the private
    ``_transport`` attribute is the narrowest way to close our end of a pipe a
    grandchild is still holding open. Closing it makes the reader's ``read()``
    see EOF, which is how the output read so far is kept rather than lost.
    """
    transport = getattr(stream, "_transport", None)
    if transport is not None:
        transport.close()


async def _read_capped(stream: asyncio.StreamReader, max_bytes: int) -> tuple[str, bool]:
    """Read ``stream`` to EOF, keeping at most ``max_bytes``.

    The whole stream is still read, so a chatty child never blocks on a full
    pipe; only the first ``max_bytes`` are kept. Decoding replaces bad bytes
    instead of raising: a cap can cut a multi-byte character in half, and a log
    line is not worth a crash.

    A cancellation is caught and the partial output is returned: ``run_proc``
    cancels a reader when an escaped grandchild keeps the pipe open past the
    kill grace period, and the bytes already read are still useful.
    """
    kept = bytearray()
    truncated = False
    try:
        while chunk := await stream.read(READ_CHUNK_BYTES):
            room = max_bytes - len(kept)
            if room > 0:
                kept += chunk[:room]
            if len(chunk) > max(room, 0):
                truncated = True
    except asyncio.CancelledError:
        pass
    return kept.decode("utf-8", "replace"), truncated
