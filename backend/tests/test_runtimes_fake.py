"""Fake-runtime edge cases not covered by the contract suite."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from gaia.domain.enums import EventType
from gaia.runtimes.approvals import InMemoryApprovalBroker
from gaia.runtimes.fake import Emit, FakeProcessRuntime, FakeRuntime, Finish, Sleep
from gaia.runtimes.support import run_with_limits
from gaia.services.git import GitService
from tests.runtime_contract import (
    RecordingSink,
    base_sha,
    build_fake_process_runtime,
    make_task,
    make_worktree_location,
)


async def test_fake_runtime_sleep_completes(
    git_repo: Path,
    git_service: GitService,
    tmp_path: Path,
) -> None:
    worktree = make_worktree_location(git_repo, tmp_path)
    task = make_task(worktree, base_sha=base_sha(git_repo))
    runtime = FakeRuntime(
        [Sleep(0.01), Finish("slept", {"ok": True})],
        InMemoryApprovalBroker(),
    )
    sink = RecordingSink()

    result = await run_with_limits(runtime, task, sink, git=git_service)

    assert result.status == "completed"
    assert result.summary == "slept"
    assert result.structured_output == {"ok": True}


async def test_fake_process_runtime_maps_message_and_failure(
    git_repo: Path,
    git_service: GitService,
    tmp_path: Path,
) -> None:
    worktree = make_worktree_location(git_repo, tmp_path)
    task = make_task(worktree, base_sha=base_sha(git_repo))
    runtime = build_fake_process_runtime(
        [
            {"type": "message", "payload": {"content": "hello"}},
            {"type": "fail", "message": "boom"},
        ],
        InMemoryApprovalBroker(),
        tmp_path,
    )
    assert isinstance(runtime, FakeProcessRuntime)
    sink = RecordingSink()

    result = await run_with_limits(runtime, task, sink, git=git_service)

    assert result.status == "failed"
    assert "boom" in (result.error or "")
    event_types = [event_type for event_type, _ in sink.events]
    assert EventType.AGENT_MESSAGE in event_types
    assert EventType.AGENT_USAGE in event_types


async def test_fake_process_runtime_maps_emit(
    git_repo: Path,
    git_service: GitService,
    tmp_path: Path,
) -> None:
    worktree = make_worktree_location(git_repo, tmp_path)
    task = make_task(worktree, base_sha=base_sha(git_repo))
    runtime = build_fake_process_runtime(
        [{"type": "emit", "event_type": EventType.AGENT_TOOL_CALL.value, "payload": {"name": "x"}}],
        InMemoryApprovalBroker(),
        tmp_path,
    )
    sink = RecordingSink()

    result = await run_with_limits(runtime, task, sink, git=git_service)

    assert result.status == "completed"
    assert EventType.AGENT_TOOL_CALL in [event_type for event_type, _ in sink.events]


class _BlockingSink:
    """An event sink that waits until a test releases it."""

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.events: list[tuple[EventType, object]] = []

    async def emit(self, type: EventType, payload: object) -> None:
        self.started.set()
        await self.release.wait()
        self.events.append((type, payload))


async def test_fake_runtime_returns_cancelled_when_cancel_lands_between_actions(
    git_repo: Path,
    tmp_path: Path,
) -> None:
    worktree = make_worktree_location(git_repo, tmp_path)
    task = make_task(worktree, base_sha=base_sha(git_repo))
    runtime = FakeRuntime(
        [Emit(EventType.AGENT_MESSAGE, {"content": "hi"}), Finish("done", None)],
        InMemoryApprovalBroker(),
    )
    sink = _BlockingSink()

    run_task = asyncio.create_task(runtime.run_task(task, sink))
    await sink.started.wait()
    await runtime.cancel(task.task_id)
    sink.release.set()
    result = await run_task

    assert result.status == "cancelled"


async def test_fake_runtime_returns_script_completed_without_finish(
    git_repo: Path,
    tmp_path: Path,
) -> None:
    worktree = make_worktree_location(git_repo, tmp_path)
    task = make_task(worktree, base_sha=base_sha(git_repo))
    runtime = FakeRuntime(
        [Emit(EventType.AGENT_MESSAGE, {"content": "hi"})],
        InMemoryApprovalBroker(),
    )

    result = await runtime.run_task(task, RecordingSink())

    assert result.status == "completed"
    assert result.summary == "script completed"


async def test_fake_runtime_ignores_an_unknown_action(
    git_repo: Path,
    tmp_path: Path,
) -> None:
    worktree = make_worktree_location(git_repo, tmp_path)
    task = make_task(worktree, base_sha=base_sha(git_repo))
    runtime = FakeRuntime([object()], InMemoryApprovalBroker())  # type: ignore[list-item]

    result = await runtime.run_task(task, RecordingSink())

    assert result.status == "completed"
    assert result.summary == "script completed"


async def test_fake_process_runtime_ignores_an_invalid_json_stdout_line(
    git_repo: Path,
    tmp_path: Path,
) -> None:
    worktree = make_worktree_location(git_repo, tmp_path)
    task = make_task(worktree, base_sha=base_sha(git_repo))
    runtime = build_fake_process_runtime([], InMemoryApprovalBroker(), tmp_path)
    assert isinstance(runtime, FakeProcessRuntime)
    runtime._task = task
    runtime._events = RecordingSink()

    await runtime._handle_line("stdout", "not-json")

    assert runtime._events.events == []


async def test_fake_process_runtime_ignores_an_unknown_action_type(
    git_repo: Path,
    tmp_path: Path,
) -> None:
    worktree = make_worktree_location(git_repo, tmp_path)
    task = make_task(worktree, base_sha=base_sha(git_repo))
    runtime = build_fake_process_runtime([], InMemoryApprovalBroker(), tmp_path)
    assert isinstance(runtime, FakeProcessRuntime)
    runtime._task = task
    runtime._events = RecordingSink()

    await runtime._handle_line("stdout", json.dumps({"type": "unknown"}))

    assert runtime._events.events == []


async def test_fake_process_runtime_direct_timeout(
    git_repo: Path,
    tmp_path: Path,
) -> None:
    worktree = make_worktree_location(git_repo, tmp_path)
    task = make_task(worktree, base_sha=base_sha(git_repo), timeout_s=0.2)
    runtime = build_fake_process_runtime(
        [{"type": "sleep", "seconds": 30}],
        InMemoryApprovalBroker(),
        tmp_path,
    )

    result = await runtime.run_task(task, RecordingSink())

    assert result.status == "timeout"


async def test_fake_process_runtime_ignores_an_invalid_emit_event(
    git_repo: Path,
    tmp_path: Path,
) -> None:
    worktree = make_worktree_location(git_repo, tmp_path)
    task = make_task(worktree, base_sha=base_sha(git_repo))
    runtime = build_fake_process_runtime([], InMemoryApprovalBroker(), tmp_path)
    assert isinstance(runtime, FakeProcessRuntime)
    runtime._task = task
    runtime._events = RecordingSink()

    await runtime._handle_line("stdout", json.dumps({"type": "emit", "event_type": "not-an-event"}))

    assert runtime._events.events == []


async def test_fake_process_runtime_cancel_handles_gone_process(
    tmp_path: Path,
) -> None:
    script = tmp_path / "script.json"
    script.write_text("[]")
    runtime = FakeProcessRuntime(script, InMemoryApprovalBroker())
    runtime._pids["task-gone"] = 2**22 + 1

    await runtime.cancel("task-gone")


async def test_fake_process_runtime_on_start_handles_gone_process(
    git_repo: Path,
    tmp_path: Path,
) -> None:
    worktree = make_worktree_location(git_repo, tmp_path)
    task = make_task(worktree, base_sha=base_sha(git_repo))
    script = tmp_path / "script.json"
    script.write_text("[]")
    runtime = FakeProcessRuntime(script, InMemoryApprovalBroker())
    runtime._task = task
    runtime._started_at = 0.0
    runtime._argv0 = "python"
    runtime._cancel_requested.add(task.task_id)

    runtime._on_start(2**22 + 1)

    assert runtime.last_pid == 2**22 + 1
