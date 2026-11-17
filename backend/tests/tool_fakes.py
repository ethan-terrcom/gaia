"""Shared fakes for the 09b tool tests.

The tools are deliberately tested against small in-memory seams, never against
the real ``srt`` binary or a real event bus. The live sandbox behaviour is
covered separately by ``tests/test_tools_live.py`` when ``GAIA_SRT_PATH`` is set.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from gaia.config.settings import Settings
from gaia.domain.enums import AgentRole, EventType
from gaia.runtimes.approvals import ApprovalBroker, InMemoryApprovalBroker
from gaia.runtimes.base import AgentTask
from gaia.security.audit import AuditSink, InMemoryAuditSink
from gaia.security.commands import CommandPolicy
from gaia.services.events import EventSink
from gaia.services.git import GitService, WorktreeLocation
from gaia.services.proc import ProcResult
from gaia.tools.registry import ToolContext


class RecordingEventSink:
    """An :class:`EventSink` that records events in memory."""

    def __init__(self) -> None:
        self.events: list[tuple[EventType, dict[str, Any]]] = []

    async def emit(self, type: EventType, payload: Mapping[str, Any]) -> None:
        self.events.append((type, dict(payload)))


class FakeCommandRunner:
    """A :class:`CommandRunner` that records calls and returns canned results."""

    def __init__(
        self,
        results: Sequence[ProcResult] | None = None,
        *,
        error: Exception | None = None,
    ) -> None:
        self.calls: list[tuple[tuple[str, ...], Path, float, Path]] = []
        self._results = list(results or [])
        self.error = error

    async def run(
        self,
        argv: Sequence[str],
        *,
        cwd: Path,
        timeout_s: float,
        task_dir: Path,
    ) -> ProcResult:
        self.calls.append((tuple(argv), cwd, timeout_s, task_dir))
        if self.error is not None:
            raise self.error
        if self._results:
            return self._results.pop(0)
        return make_proc_result(argv, cwd=cwd, returncode=0)


def make_proc_result(
    argv: Sequence[str],
    *,
    cwd: Path,
    returncode: int = 0,
    stdout: str = "",
    stderr: str = "",
    stdout_truncated: bool = False,
    stderr_truncated: bool = False,
    timed_out: bool = False,
) -> ProcResult:
    return ProcResult(
        argv=tuple(argv),
        cwd=Path(cwd),
        pid=1,
        returncode=returncode,
        stdout=stdout,
        stderr=stderr,
        stdout_truncated=stdout_truncated,
        stderr_truncated=stderr_truncated,
        duration_s=0.0,
        timed_out=timed_out,
    )


def make_tool_context(
    root: Path,
    *,
    task_dir: Path | None = None,
    git: GitService | None = None,
    policy: CommandPolicy | None = None,
    approvals: ApprovalBroker | None = None,
    audit: AuditSink | None = None,
    events: EventSink | None = None,
    commands: Any | None = None,
    rg_path: Path | None = None,
    actor: str = "agent:junior-1",
    extra_allow: tuple[tuple[str, ...], ...] = (),
    approval_timeout_s: float = 600,
    auto_approve: Callable[[], bool] | None = None,
    extra_allow_source: Callable[[], Awaitable[tuple[tuple[str, ...], ...]]] | None = None,
) -> ToolContext:
    """Build a real :class:`ToolContext` around ``root``, with fake services."""
    root = root.resolve()
    task_dir = task_dir.resolve() if task_dir is not None else root / "task-tmp"
    task_dir.mkdir(parents=True, exist_ok=True)
    if git is None:
        git = GitService(Settings(data_dir=root.parent / "gaia-data"))
    task = AgentTask(
        task_id="task-tools",
        run_id="run-tools",
        run_step_id=None,
        workspace_id="ws-tools",
        agent_id="agent-junior-1",
        role=AgentRole.JUNIOR,
        instructions="use tools",
        worktree=WorktreeLocation(work_tree=root, git_dir=root / ".git"),
        base_sha="0" * 40,
        extra_allow=extra_allow,
    )
    return ToolContext(
        task=task,
        git=git,
        policy=policy or CommandPolicy.from_default(),
        approvals=approvals or InMemoryApprovalBroker(),
        audit=audit or InMemoryAuditSink(),
        events=events or RecordingEventSink(),
        commands=commands or FakeCommandRunner(),
        task_dir=task_dir,
        actor=actor,
        rg_path=rg_path,
        approval_timeout_s=approval_timeout_s,
        auto_approve=auto_approve,
        extra_allow_source=extra_allow_source,
    )
