"""The command tool and its one production runner (doc 09b task 3).

``run_command`` is a tripwire behind the OS sandbox: it evaluates argv with the
command policy, asks for approval when the policy says ``ask``, then launches
through :class:`CommandRunner`. The only production runner is
:class:`SrtCommandRunner`, which wraps the command with the 09a sandbox profile.
There is deliberately no unsandboxed runner in ``gaia/``; building a
:class:`~gaia.tools.registry.ToolContext` without a runner is a type error.
"""

from __future__ import annotations

import os
import shutil
import uuid
from collections.abc import Sequence
from pathlib import Path
from typing import Protocol, cast

from pydantic import BaseModel, Field

from gaia.config.settings import Settings
from gaia.runtimes.support import build_agent_env
from gaia.security.sandbox import SandboxSpec, wrap_argv, write_srt_settings
from gaia.services.proc import ProcResult, run_proc
from gaia.tools.registry import Tool, ToolContext, ToolDecision, ToolInput, ToolResult, approve

#: The longest a single ``run_command`` may run. Runtimes that impose their own
#: tool-call timeout (the Agent SDK's CLI) must allow at least this plus the
#: approval wait (lead review 10c, C3).
MAX_COMMAND_TIMEOUT_S = 1800

#: Maximum output bytes a command may buffer per stream.
COMMAND_OUTPUT_BYTES = 64_000


class RunCommandInput(ToolInput):
    argv: list[str] = Field(min_length=1, max_length=256)
    timeout_s: int = Field(default=300, ge=1, le=MAX_COMMAND_TIMEOUT_S)


class CommandRunner(Protocol):
    """Launch one command. Tests use a fake; production uses ``srt``."""

    async def run(
        self,
        argv: Sequence[str],
        *,
        cwd: Path,
        timeout_s: float,
        task_dir: Path,
    ) -> ProcResult:
        """Run ``argv`` under the OS sandbox and return its result."""
        ...


async def _run_command_handler(input: BaseModel, ctx: ToolContext) -> ToolResult:
    model = cast(RunCommandInput, input)
    decision = ctx.policy.evaluate(model.argv, extra_allow=ctx.task.extra_allow)
    if decision.action == "deny":
        return ToolResult(
            False,
            f"denied by policy: {decision.reason}",
            decision="deny",
            data={"reason": decision.reason},
        )
    decision_value: ToolDecision
    if decision.action == "ask":
        answer = await approve(
            ctx,
            {"argv": model.argv, "cwd": str(ctx.root), "reason": decision.reason},
            decision.reason,
            tool="run_command",
        )
        if not answer.approved:
            return ToolResult(
                False,
                f"not approved: {answer.reason}",
                decision="ask_rejected",
                data={"reason": answer.reason},
            )
        decision_value = "ask_approved"
    else:
        decision_value = "allow"

    result = await ctx.commands.run(
        model.argv,
        cwd=ctx.root,
        timeout_s=float(model.timeout_s),
        task_dir=ctx.task_dir,
    )
    return ToolResult(
        ok=result.returncode == 0 and not result.timed_out,
        content=_format_proc_result(result, timeout_s=model.timeout_s),
        decision=decision_value,
        data={
            "returncode": result.returncode,
            "timed_out": result.timed_out,
            "reason": result.describe(),
        },
    )


class SrtCommandRunner:
    """Run every command through the 09a sandbox profile."""

    def __init__(
        self,
        settings: Settings,
        *,
        srt_path: Path,
        allowed_domains: tuple[str, ...] = (),
    ) -> None:
        self.settings = settings
        self.srt_path = srt_path
        self.allowed_domains = allowed_domains

    async def run(
        self,
        argv: Sequence[str],
        *,
        cwd: Path,
        timeout_s: float,
        task_dir: Path,
    ) -> ProcResult:
        spec = SandboxSpec(
            worktree=cwd,
            tmp_dir=task_dir,
            data_dir=self.settings.data_dir,
            toolchain_read=self.settings.sandbox_toolchain_read,
            allowed_domains=self.allowed_domains,
            gateway=None,
        )
        # The settings file lives under Gaia's data dir, never in ``task_dir``:
        # the sandboxed command can write to ``task_dir``, and a process left
        # behind by one command could otherwise rewrite the next command's
        # sandbox rules (lead review S1). ``data_dir`` is read- and write-denied
        # inside the sandbox. One fresh directory per command, removed after.
        settings_dir = sandbox_settings_dir(self.settings)
        try:
            settings_path = write_srt_settings(spec, settings_dir)
            wrapped = wrap_argv(self.srt_path, settings_path, argv)
            return await run_proc(
                wrapped,
                cwd=cwd,
                timeout_s=timeout_s,
                env=command_env(self.settings, task_dir),
                max_output_bytes=COMMAND_OUTPUT_BYTES,
            )
        finally:
            shutil.rmtree(settings_dir, ignore_errors=True)


def sandbox_settings_dir(settings: Settings) -> Path:
    """A new, owner-only directory for one command's srt settings, under ``data_dir``."""
    parent = Path(settings.data_dir) / "sandbox"
    parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    directory = parent / uuid.uuid4().hex
    directory.mkdir(mode=0o700)
    return directory


def command_env(settings: Settings, task_dir: Path) -> dict[str, str]:
    """The environment for a sandboxed command, built from nothing.

    It never reads ``os.environ`` and never carries a provider key. ``PATH``
    comes from :attr:`Settings.agent_path`, not from the ambient process.
    """
    env = build_agent_env(
        home=task_dir,
        gateway_url=None,
        run_token=None,
        provider=None,
        path=os.pathsep.join(settings.agent_path),
    )
    env["UV_NO_SYNC"] = "1"
    env["UV_CACHE_DIR"] = str(task_dir / "uv-cache")
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    uv_python_dir = _readable_uv_python_dir(settings)
    if uv_python_dir is not None:
        env["UV_PYTHON_INSTALL_DIR"] = str(uv_python_dir)
    return env


def _readable_uv_python_dir(settings: Settings) -> Path | None:
    """:attr:`Settings.sandbox_uv_python_dir`, if it exists and the sandbox may read it.

    A directory outside every ``sandbox_toolchain_read`` entry would be denied
    inside the sandbox, which is worse than uv's default, so it is left unset.
    """
    candidate = settings.sandbox_uv_python_dir
    if candidate is None or not candidate.is_dir():
        return None
    readable = any(candidate.is_relative_to(root) for root in settings.sandbox_toolchain_read)
    return candidate if readable else None


def _format_proc_result(result: ProcResult, *, timeout_s: int) -> str:
    first = f"timed out after {timeout_s}s" if result.timed_out else f"exit={result.returncode}"
    parts = [first, "--- stdout ---"]
    if result.stdout:
        parts.append(result.stdout)
    if result.stdout_truncated:
        parts.append("[output truncated]")
    parts.append("--- stderr ---")
    if result.stderr:
        parts.append(result.stderr)
    if result.stderr_truncated:
        parts.append("[output truncated]")
    return "\n".join(parts)


run_command = Tool(
    name="run_command",
    description=(
        "Run a command in the worktree under the Gaia sandbox. "
        "argv is a list of strings, never a shell string. "
        "The command policy decides allow/deny/ask."
    ),
    input_model=RunCommandInput,
    handler=_run_command_handler,
    mutates=True,
)
