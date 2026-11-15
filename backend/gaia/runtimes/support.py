"""Shared runtime machinery that must be identical in every runtime.

These are the parts of doc 10a that a runtime author must not re-implement:
the scrubbed agent environment, ``files_changed`` from git rather than from the
agent's own claim, and the timeout/cancellation wrapper.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from pathlib import Path
from typing import Literal

import structlog
from pydantic import SecretStr

from gaia.domain.enums import EventType
from gaia.runtimes.base import AgentResult, AgentRuntime, AgentTask, RuntimeUsage
from gaia.security.redaction import redact
from gaia.services.events import EventSink
from gaia.services.git import GitService, WorktreeLocation

#: The fixed system path every agent process gets. Deliberately not read from
#: the parent: a caller could have planted a credential-stealing shim there.
AGENT_PATH = "/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"

#: Error messages are capped before they become an ``AgentResult.error``.
_MAX_ERROR_CHARS = 500

#: Longest wait for a runtime's own ``cancel`` after a timeout or an outer cancel.
CANCEL_GRACE_S = 10.0

logger = structlog.get_logger(__name__)


def build_agent_env(
    *,
    home: Path,
    gateway_url: str | None,
    run_token: SecretStr | None,
    provider: Literal["anthropic", "deepseek"] | None,
    path: str = AGENT_PATH,
) -> dict[str, str]:
    """Build the environment for an agent process from scratch.

    The result contains exactly ``PATH``, ``HOME``, ``LANG`` and ``TMPDIR``,
    plus the provider gateway variables when ``provider`` is set. It never reads
    ``os.environ``: the parent's real provider keys and every other variable are
    absent by construction, not by denylist.
    """
    env = {
        "PATH": path,
        "HOME": str(home),
        "LANG": "C.UTF-8",
        "TMPDIR": str(home),
    }
    if provider is None:
        return env
    if gateway_url is None or run_token is None:
        raise ValueError("gateway_url and run_token are required when provider is set")
    if provider == "anthropic":
        env["ANTHROPIC_BASE_URL"] = gateway_url
        env["ANTHROPIC_API_KEY"] = run_token.get_secret_value()
    else:
        env["OPENAI_BASE_URL"] = gateway_url
        env["OPENAI_API_KEY"] = run_token.get_secret_value()
    return env


async def files_changed(
    git: GitService, location: WorktreeLocation, base_sha: str
) -> tuple[str, ...]:
    """Return every path that changed since ``base_sha`` (requirement 6).

    The result comes from git, never from a runtime. ``changed_paths`` is the
    single git verb that knows how to combine tracked, staged, unstaged and
    untracked paths.
    """
    return await git.changed_paths(location, base_sha)


async def run_with_limits(
    runtime: AgentRuntime,
    task: AgentTask,
    events: EventSink,
    *,
    git: GitService,
    now: Callable[[], float] = time.monotonic,
) -> AgentResult:
    """Run ``runtime`` under the limits every runtime must respect.

    Emits a starting ``agent.message`` and a final ``agent.usage`` around the
    call, enforces ``task.timeout_s``, converts unexpected exceptions to
    ``status="failed"`` with a redacted error, and computes ``files_changed``
    from git so no runtime's own claim is trusted.

    A ``CancelledError`` from outside is not converted to a result: the caller
    cancelled the whole run. The runtime is still cancelled and the exception
    re-raised.
    """
    started = now()
    await events.emit(
        EventType.AGENT_MESSAGE,
        {"role": task.role.value, "content": task.instructions},
    )
    try:
        async with asyncio.timeout(task.timeout_s):
            result = await runtime.run_task(task, events)
    except TimeoutError:
        await _cancel_bounded(runtime, task.task_id)
        return _timeout_result(
            await files_changed(git, task.worktree, task.base_sha),
            await _emit_usage(events, RuntimeUsage(), now() - started),
        )
    except asyncio.CancelledError:
        await _cancel_bounded(runtime, task.task_id)
        raise
    except Exception as exc:
        return _failed_result(
            error=_redacted_error(exc),
            files=await files_changed(git, task.worktree, task.base_sha),
            usage=await _emit_usage(events, RuntimeUsage(), now() - started),
        )

    files = await files_changed(git, task.worktree, task.base_sha)
    await _emit_usage(events, result.usage, now() - started)
    return AgentResult(
        status=result.status,
        summary=result.summary,
        structured_output=result.structured_output,
        files_changed=files,
        usage=result.usage,
        error=result.error,
        transcript_ref=result.transcript_ref,
    )


async def _cancel_bounded(runtime: AgentRuntime, task_id: str) -> None:
    """Ask ``runtime`` to cancel, but never wait longer than :data:`CANCEL_GRACE_S`.

    A runtime whose ``cancel`` hangs must not turn a wall-clock timeout into an
    unbounded wait (lead review 10a, S1). The task's processes are still reaped
    at the next startup by the orphan registry.
    """
    try:
        async with asyncio.timeout(CANCEL_GRACE_S):
            await runtime.cancel(task_id)
    except TimeoutError:
        logger.warning("runtime.cancel_timed_out", runtime=runtime.name, task_id=task_id)


async def _emit_usage(events: EventSink, usage: RuntimeUsage, duration_s: float) -> RuntimeUsage:
    """Publish the final usage event and return the usage value unchanged."""
    await events.emit(
        EventType.AGENT_USAGE,
        {
            "input_tokens": usage.input_tokens,
            "output_tokens": usage.output_tokens,
            "cache_tokens": usage.cache_tokens,
            "cost_usd": str(usage.cost_usd),
            "duration_s": round(duration_s, 3),
        },
    )
    return usage


def _redacted_error(exc: Exception) -> str:
    """A short, redacted error string for an unexpected exception."""
    message = str(exc) or type(exc).__name__
    return redact(message[:_MAX_ERROR_CHARS])


def _timeout_result(files: tuple[str, ...], usage: RuntimeUsage) -> AgentResult:
    return AgentResult(
        status="timeout",
        summary="task timed out",
        structured_output=None,
        files_changed=files,
        usage=usage,
        error=None,
        transcript_ref=None,
    )


def _failed_result(*, error: str, files: tuple[str, ...], usage: RuntimeUsage) -> AgentResult:
    return AgentResult(
        status="failed",
        summary="task failed",
        structured_output=None,
        files_changed=files,
        usage=usage,
        error=error,
        transcript_ref=None,
    )
