"""Fake runtimes for tests, demos and ``gaia dev run-task`` (doc 10a).

The fakes are production code on purpose: they exercise the same
:class:`AgentRuntime` seam and the same ``run_with_limits`` wrapper as a real
runtime, but their behaviour is a script instead of a model.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import signal
import sys
import tempfile
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from gaia.domain.enums import ApprovalKind, EventType
from gaia.runtimes.approvals import ApprovalBroker, ApprovalDecision
from gaia.runtimes.base import AgentResult, AgentTask, RuntimeUsage
from gaia.runtimes.orphans import OrphanRegistry
from gaia.runtimes.support import build_agent_env
from gaia.security.paths import confine
from gaia.security.redaction import redact
from gaia.services.proc import stream_proc

FAKE_AGENT_PATH = Path(__file__).with_name("fake_agent.py")


@dataclass(frozen=True)
class Emit:
    """Publish one event to the sink."""

    type: EventType
    payload: Mapping[str, Any]


@dataclass(frozen=True)
class WriteFile:
    """Write one file, confined under the task worktree."""

    path: str
    content: str


@dataclass(frozen=True)
class RequestApproval:
    """Ask the broker to approve one argv list."""

    argv: Sequence[str]


@dataclass(frozen=True)
class Sleep:
    """Wait for ``seconds``, or until cancelled."""

    seconds: float


@dataclass(frozen=True)
class Fail:
    """Raise an exception, which ``run_with_limits`` turns into a failed result."""

    message: str


@dataclass(frozen=True)
class Finish:
    """Return a completed result.

    ``files_changed`` is intentionally a field here: the contract test proves
    that ``run_with_limits`` replaces this agent claim with git's answer.
    """

    summary: str
    structured_output: dict[str, Any] | None
    files_changed: tuple[str, ...] = ()


FakeAction = Emit | WriteFile | RequestApproval | Sleep | Fail | Finish


class FakeRuntime:
    """An in-process runtime driven by a list of :class:`FakeAction` values."""

    name = "fake"

    def __init__(self, script: Sequence[FakeAction], broker: ApprovalBroker) -> None:
        self.script = tuple(script)
        self.broker = broker
        self._cancel_events: dict[str, asyncio.Event] = {}

    async def run_task(self, task: AgentTask, events: Any) -> AgentResult:
        """Execute ``script`` in order until :class:`Finish` or cancellation."""
        cancel_event = asyncio.Event()
        self._cancel_events[task.task_id] = cancel_event
        denial_notes: list[str] = []
        try:
            for action in self.script:
                if cancel_event.is_set():
                    return _cancelled_result(denial_notes)
                if isinstance(action, Sleep):
                    if await _sleep_until(cancel_event, action.seconds):
                        return _cancelled_result(denial_notes)
                elif isinstance(action, Emit):
                    await events.emit(action.type, action.payload)
                elif isinstance(action, WriteFile):
                    _write_file(task.worktree.work_tree, action.path, action.content)
                elif isinstance(action, RequestApproval):
                    decision = await self.broker.request(
                        task,
                        kind=ApprovalKind.COMMAND,
                        payload={"argv": tuple(action.argv)},
                        timeout_s=task.timeout_s,
                    )
                    if not decision.approved:
                        denial_notes.append(
                            f"approval denied for {' '.join(action.argv)}: {decision.reason}"
                        )
                elif isinstance(action, Fail):
                    raise RuntimeError(action.message)
                elif isinstance(action, Finish):
                    return _completed_result(action, denial_notes)
            return AgentResult(
                status="completed",
                summary=_with_denials("script completed", denial_notes),
                structured_output=None,
                files_changed=(),
                usage=RuntimeUsage(),
                error=None,
                transcript_ref=None,
            )
        finally:
            self._cancel_events.pop(task.task_id, None)

    async def cancel(self, task_id: str) -> None:
        """Wake :meth:`run_task` so it returns ``status="cancelled"``."""
        event = self._cancel_events.get(task_id)
        if event is not None:
            event.set()


class FakeProcessRuntime:
    """A real subprocess running :mod:`gaia.runtimes.fake_agent`.

    The child writes files and prints JSON lines; this runtime maps those lines
    to events and routes approval lines through ``broker``. It records the
    child's pid in ``orphans`` while it is alive so a crash can be cleaned up.
    """

    name = "fake_process"

    def __init__(
        self,
        script_path: Path,
        broker: ApprovalBroker,
        *,
        orphans: OrphanRegistry | None = None,
    ) -> None:
        self.script_path = Path(script_path)
        self.broker = broker
        self.orphans = orphans
        self._cancel_requested: set[str] = set()
        self._pids: dict[str, int] = {}
        self.last_pid: int | None = None

    async def run_task(self, task: AgentTask, events: Any) -> AgentResult:
        """Run the fake agent in the worktree and map its JSON lines to events."""
        self._task = task
        self._events = events
        self._denial_notes: list[str] = []
        self._finish_summary: str | None = None
        self._finish_structured: dict[str, Any] | None = None
        self._claimed_files: tuple[str, ...] = ()
        self._active_pid: int | None = None
        home = _make_agent_home()
        self._decision_dir = _ensure_dir(home / "approvals")
        self._started_at = time.time()
        self._argv0 = sys.executable
        was_cancelled = False
        try:
            proc_result = await stream_proc(
                [self._argv0, str(FAKE_AGENT_PATH), str(self.script_path)],
                cwd=task.worktree.work_tree,
                env=build_agent_env(home=home, gateway_url=None, run_token=None, provider=None),
                on_line=self._handle_line,
                timeout_s=task.timeout_s,
                on_start=self._on_start,
            )
            was_cancelled = task.task_id in self._cancel_requested
        finally:
            self._unregister_orphan()
            self._pids.pop(task.task_id, None)
            self._cancel_requested.discard(task.task_id)
            _remove_tree(home)

        if proc_result.timed_out:
            return AgentResult(
                status="timeout",
                summary="fake process timed out",
                structured_output=None,
                files_changed=(),
                usage=RuntimeUsage(),
                error=None,
                transcript_ref=None,
            )
        if was_cancelled:
            return AgentResult(
                status="cancelled",
                summary="cancelled",
                structured_output=None,
                files_changed=(),
                usage=RuntimeUsage(),
                error=None,
                transcript_ref=None,
            )
        if proc_result.returncode != 0:
            return AgentResult(
                status="failed",
                summary="fake process failed",
                structured_output=None,
                files_changed=(),
                usage=RuntimeUsage(),
                error=redact(proc_result.stderr[-500:] or "fake agent exited non-zero"),
                transcript_ref=None,
            )
        summary = self._finish_summary or "fake process completed"
        summary = _with_denials(summary, self._denial_notes)
        return AgentResult(
            status="completed",
            summary=summary,
            structured_output=self._finish_structured,
            files_changed=self._claimed_files,
            usage=RuntimeUsage(),
            error=None,
            transcript_ref=None,
        )

    async def cancel(self, task_id: str) -> None:
        """Kill the fake-agent process group for ``task_id``, if it is running."""
        self._cancel_requested.add(task_id)
        pid = self._pids.get(task_id)
        if pid is not None:
            try:
                os.killpg(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

    def _unregister_orphan(self) -> None:
        """Remove the active pid from the orphan registry, if it was recorded."""
        if self.orphans is not None and self._active_pid is not None:
            self.orphans.remove(self._active_pid)

    def _on_start(self, pid: int) -> None:
        """Record the child pid (= pgid) for orphan tracking and cancellation."""
        self._active_pid = pid
        self.last_pid = pid
        self._pids[self._task.task_id] = pid
        if self.orphans is not None:
            self.orphans.add(
                pid=pid,
                task_id=self._task.task_id,
                started_at=self._started_at,
                argv0=self._argv0,
            )
        if self._task.task_id in self._cancel_requested:
            try:
                os.killpg(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

    async def _handle_line(self, stream: str, line: str) -> None:
        """Handle one JSON line printed by the fake agent."""
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            if stream == "stderr":
                await self._events.emit(EventType.AGENT_MESSAGE, {"stream": stream, "line": line})
            return
        action_type = data.get("type")
        if action_type == "message":
            await self._events.emit(EventType.AGENT_MESSAGE, dict(data.get("payload") or {}))
        elif action_type == "emit":
            try:
                event_type = EventType(str(data.get("event_type")))
            except ValueError:
                return
            await self._events.emit(event_type, dict(data.get("payload") or {}))
        elif action_type == "write":
            await self._events.emit(
                EventType.AGENT_TOOL_CALL,
                {"name": "write_file", "path": data.get("path"), "content": data.get("content")},
            )
            await self._events.emit(
                EventType.AGENT_TOOL_RESULT,
                {"name": "write_file", "path": data.get("path"), "summary": "written"},
            )
        elif action_type == "env":
            environment = data.get("environment") or {}
            self._env = dict(environment)
            await self._events.emit(EventType.AGENT_MESSAGE, {"environment": environment})
        elif action_type == "approval":
            argv = data.get("argv") or []
            request_id = str(data.get("request_id") or "")
            decision = await self.broker.request(
                self._task,
                kind=ApprovalKind.COMMAND,
                payload={"argv": list(argv)},
                timeout_s=self._task.timeout_s,
            )
            _write_decision_file(self._decision_dir, request_id, decision)
            if not decision.approved:
                self._denial_notes.append(
                    f"approval denied for {' '.join(map(str, argv))}: {decision.reason}"
                )
        elif action_type == "finish":
            self._finish_summary = str(data.get("summary") or "")
            self._finish_structured = data.get("structured_output")
            self._claimed_files = tuple(data.get("files_changed") or ())


def _completed_result(action: Finish, denial_notes: list[str]) -> AgentResult:
    """Build a completed result from a :class:`Finish` action."""
    return AgentResult(
        status="completed",
        summary=_with_denials(action.summary, denial_notes),
        structured_output=action.structured_output,
        files_changed=action.files_changed,
        usage=RuntimeUsage(),
        error=None,
        transcript_ref=None,
    )


def _cancelled_result(denial_notes: list[str]) -> AgentResult:
    """Build the result returned when ``cancel`` interrupted the script."""
    return AgentResult(
        status="cancelled",
        summary=_with_denials("cancelled", denial_notes),
        structured_output=None,
        files_changed=(),
        usage=RuntimeUsage(),
        error=None,
        transcript_ref=None,
    )


def _with_denials(summary: str, denial_notes: list[str]) -> str:
    """Append any denial notes to ``summary``, keeping the result one string."""
    if not denial_notes:
        return summary
    return f"{summary}; {', '.join(denial_notes)}"


async def _sleep_until(event: asyncio.Event, seconds: float) -> bool:
    """Sleep until ``event`` is set or ``seconds`` elapse. True means cancelled."""
    try:
        await asyncio.wait_for(event.wait(), timeout=seconds)
    except TimeoutError:
        return False
    return True


def _write_file(root: Path, user_path: str, content: str) -> Path:
    """Confine and write one file, in a plain function for ruff's ASYNC rules."""
    path = confine(root, user_path)
    path.write_text(content)
    return path


def _make_agent_home() -> Path:
    """Create the per-task temp home for one fake process."""
    return Path(tempfile.mkdtemp(prefix="gaia-runtime-"))


def _ensure_dir(path: Path) -> Path:
    """Create ``path`` and return it."""
    path.mkdir(parents=True, exist_ok=True)
    return path


def _write_decision_file(decision_dir: Path, request_id: str, decision: ApprovalDecision) -> None:
    """Write the broker's decision where ``fake_agent.py`` will poll for it."""
    path = decision_dir / f"{request_id}.json"
    path.write_text(
        json.dumps(
            {
                "approved": decision.approved,
                "reason": decision.reason,
            }
        )
    )


def _remove_tree(path: Path) -> None:
    """Remove a temp directory tree, ignoring any filesystem race."""
    shutil.rmtree(path, ignore_errors=True)
