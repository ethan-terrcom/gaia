"""The doc 10a runtime contract, run against both fake runtimes.

Every runtime-facing test goes through :func:`gaia.runtimes.support.run_with_limits`,
so timeout, cancellation, event ordering and git-derived ``files_changed`` are
proved once for the in-process fake and once for the subprocess fake.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from gaia.db.ids import uuid7
from gaia.domain.enums import EventType
from gaia.runtimes.approvals import InMemoryApprovalBroker
from gaia.runtimes.base import AgentResult, AgentRuntime, AgentTask
from gaia.runtimes.fake import (
    Emit,
    FakeAction,
    FakeProcessRuntime,
    Finish,
    RequestApproval,
    Sleep,
    WriteFile,
)
from gaia.runtimes.orphans import OrphanRegistry, is_ours
from gaia.runtimes.support import AGENT_PATH, run_with_limits
from gaia.services.git import GitService
from tests.git_repo import git
from tests.runtime_contract import (
    PLANTED_ANTHROPIC_KEY,
    RecordingSink,
    base_sha,
    build_fake_process_runtime,
    build_fake_runtime,
    make_task,
    make_worktree_location,
    to_json_actions,
    wait_for_pending,
    wait_until,
)


@pytest.fixture
def worktree(git_repo: Path, tmp_path: Path):
    """A linked worktree whose base SHA is the fixture repo's first commit."""
    return make_worktree_location(git_repo, tmp_path)


@pytest.fixture(params=["fake", "fake_process"])
def runtime_name(request: pytest.FixtureRequest) -> str:
    return str(request.param)


def make_runtime(
    runtime_name: str,
    actions: Sequence[FakeAction],
    broker: InMemoryApprovalBroker,
    tmp_path: Path,
    *,
    orphans: OrphanRegistry | None = None,
) -> AgentRuntime:
    """Build the requested fake runtime from one common action list."""
    if runtime_name == "fake":
        return build_fake_runtime(actions, broker, orphans)
    return build_fake_process_runtime(to_json_actions(actions), broker, tmp_path, orphans)


async def _run(
    runtime_name: str,
    actions: Sequence[FakeAction],
    task: AgentTask,
    git_service: GitService,
    tmp_path: Path,
    broker: InMemoryApprovalBroker,
    *,
    orphans: OrphanRegistry | None = None,
) -> tuple[AgentRuntime, AgentResult, RecordingSink]:
    runtime = make_runtime(runtime_name, actions, broker, tmp_path, orphans=orphans)
    sink = RecordingSink()
    result = await run_with_limits(runtime, task, sink, git=git_service)
    return runtime, result, sink


async def test_timeout_returns_timeout_and_the_process_is_gone(
    runtime_name: str,
    worktree: Any,
    git_repo: Path,
    git_service: GitService,
    tmp_path: Path,
) -> None:
    broker = InMemoryApprovalBroker()
    task = make_task(
        worktree,
        base_sha=base_sha(git_repo),
        timeout_s=0.2,
        instructions="sleep past the deadline",
    )
    runtime, result, _ = await _run(runtime_name, [Sleep(30)], task, git_service, tmp_path, broker)

    assert result.status == "timeout"
    assert result.error is None
    if isinstance(runtime, FakeProcessRuntime):
        assert runtime.last_pid is not None
        _assert_pid_gone(runtime.last_pid)


async def test_cancel_mid_run_returns_cancelled_within_5_s(
    runtime_name: str,
    worktree: Any,
    git_repo: Path,
    git_service: GitService,
    tmp_path: Path,
) -> None:
    broker = InMemoryApprovalBroker()
    task = make_task(
        worktree,
        base_sha=base_sha(git_repo),
        timeout_s=60,
        instructions="run until cancelled",
    )
    runtime = make_runtime(runtime_name, [Sleep(30)], broker, tmp_path)
    sink = RecordingSink()

    started = time.monotonic()
    run_task = asyncio.create_task(run_with_limits(runtime, task, sink, git=git_service))
    try:
        if isinstance(runtime, FakeProcessRuntime):
            await wait_until(lambda: runtime.last_pid is not None)
        else:
            await wait_until(lambda: task.task_id in runtime._cancel_events)  # type: ignore[attr-defined]
        await runtime.cancel(task.task_id)
        result = await asyncio.wait_for(run_task, timeout=5)
    finally:
        if not run_task.done():
            run_task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await run_task

    assert result.status == "cancelled"
    assert time.monotonic() - started < 5
    if isinstance(runtime, FakeProcessRuntime) and runtime.last_pid is not None:
        _assert_pid_gone(runtime.last_pid)


async def test_env_scrub_leaves_no_planted_or_provider_key(
    runtime_name: str,
    worktree: Any,
    git_repo: Path,
    git_service: GitService,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if runtime_name != "fake_process":
        pytest.skip("env scrub is a FakeProcessRuntime contract test")
    monkeypatch.setenv("ANTHROPIC_API_KEY", PLANTED_ANTHROPIC_KEY)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-planted-openai-000000000000000000")

    broker = InMemoryApprovalBroker()
    runtime = build_fake_process_runtime([{"type": "env"}], broker, tmp_path)
    task = make_task(worktree, base_sha=base_sha(git_repo))
    sink = RecordingSink()

    result = await run_with_limits(runtime, task, sink, git=git_service)

    assert result.status == "completed"
    environment = _find_environment(sink)
    assert "ANTHROPIC_API_KEY" not in environment
    assert "OPENAI_API_KEY" not in environment
    assert environment["PATH"] == AGENT_PATH
    assert environment["HOME"] == environment["TMPDIR"]
    for _, payload in sink.events:
        assert PLANTED_ANTHROPIC_KEY not in json.dumps(payload)


async def test_files_changed_matches_git_and_ignores_the_agent_claim(
    runtime_name: str,
    worktree: Any,
    git_repo: Path,
    git_service: GitService,
    tmp_path: Path,
) -> None:
    work_tree = Path(worktree.work_tree)
    _write_committed_staged_unstaged(work_tree)
    actions: list[FakeAction] = [
        WriteFile("untracked.txt", "untracked\n"),
        Finish("done", None, files_changed=("agent-claimed.txt",)),
    ]
    broker = InMemoryApprovalBroker()
    task = make_task(worktree, base_sha=base_sha(git_repo))

    _, result, _ = await _run(runtime_name, actions, task, git_service, tmp_path, broker)

    assert result.status == "completed"
    assert result.files_changed == (
        "committed.txt",
        "staged.txt",
        "unstaged.txt",
        "untracked.txt",
    )
    assert "agent-claimed.txt" not in result.files_changed


async def test_events_are_emitted_in_order(
    runtime_name: str,
    worktree: Any,
    git_repo: Path,
    git_service: GitService,
    tmp_path: Path,
) -> None:
    actions: list[FakeAction] = [
        Emit(EventType.AGENT_TOOL_CALL, {"name": "run_pytest"}),
        Emit(EventType.AGENT_MESSAGE, {"content": "hello"}),
        Finish("done", None),
    ]
    broker = InMemoryApprovalBroker()
    task = make_task(worktree, base_sha=base_sha(git_repo))

    _, _, sink = await _run(runtime_name, actions, task, git_service, tmp_path, broker)

    assert [event_type for event_type, _ in sink.events] == [
        EventType.AGENT_MESSAGE,
        EventType.AGENT_TOOL_CALL,
        EventType.AGENT_MESSAGE,
        EventType.AGENT_USAGE,
    ]


async def test_in_turn_approval_approved_and_denied(
    runtime_name: str,
    worktree: Any,
    git_repo: Path,
    git_service: GitService,
    tmp_path: Path,
) -> None:
    actions: list[FakeAction] = [
        RequestApproval(["pytest", "-q"]),
        RequestApproval(["pytest", "-q"]),
        Finish("done", None),
    ]
    broker = InMemoryApprovalBroker()
    task = make_task(worktree, base_sha=base_sha(git_repo))
    runtime = make_runtime(runtime_name, actions, broker, tmp_path)
    sink = RecordingSink()

    run_task = asyncio.create_task(run_with_limits(runtime, task, sink, git=git_service))
    first = await wait_for_pending(broker)
    broker.decide(first, True, "approved")
    second = await wait_for_pending(broker, exclude={first})
    broker.decide(second, False, "denied")
    result = await asyncio.wait_for(run_task, timeout=5)

    assert result.status == "completed"
    assert "denied" in result.summary
    assert broker.pending() == ()


async def test_orphan_registry_reaps_a_live_fake_agent(
    runtime_name: str,
    worktree: Any,
    git_repo: Path,
    git_service: GitService,
    tmp_path: Path,
) -> None:
    if runtime_name != "fake_process":
        pytest.skip("orphan reaping is a FakeProcessRuntime contract test")
    registry = OrphanRegistry(tmp_path / "runtime-pids.json")
    broker = InMemoryApprovalBroker()
    actions = [Sleep(30)]
    task = make_task(worktree, base_sha=base_sha(git_repo), timeout_s=60)
    runtime = make_runtime(runtime_name, actions, broker, tmp_path, orphans=registry)
    assert isinstance(runtime, FakeProcessRuntime)
    sink = RecordingSink()

    run_task = asyncio.create_task(run_with_limits(runtime, task, sink, git=git_service))
    try:
        await wait_until(lambda: runtime.last_pid is not None)
        assert runtime.last_pid is not None
        assert _registry_has_pid(registry, runtime.last_pid)

        async def ours(entry: Any) -> bool:
            return bool(entry.pid == runtime.last_pid)

        await registry.reap_stale(is_ours=ours)
        _assert_pid_gone(runtime.last_pid)
        result = await asyncio.wait_for(run_task, timeout=5)
    finally:
        if not run_task.done():
            run_task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await run_task

    assert result.status == "failed"
    assert not _registry_has_pid(registry, runtime.last_pid)


async def test_orphan_registry_never_kills_an_unverified_pid(tmp_path: Path) -> None:
    registry = OrphanRegistry(tmp_path / "runtime-pids.json")
    registry.add(
        pid=os.getpid(),
        task_id=uuid7(),
        started_at=time.time() - 10_000,
        argv0="definitely-not-the-test-process",
    )

    await registry.reap_stale(is_ours=is_ours)

    # The test process is still alive, and the entry was dropped anyway.
    os.kill(os.getpid(), 0)
    assert not _registry_has_pid(registry, os.getpid())


def _write_committed_staged_unstaged(work_tree: Path) -> None:
    (work_tree / "committed.txt").write_text("committed\n")
    git(work_tree, "add", "committed.txt")
    git(work_tree, "commit", "-q", "-m", "commit a file")
    (work_tree / "staged.txt").write_text("staged\n")
    git(work_tree, "add", "staged.txt")
    (work_tree / "unstaged.txt").write_text("unstaged\n")


def _find_environment(sink: RecordingSink) -> dict[str, str]:
    for _, payload in sink.events:
        if "environment" in payload:
            return dict(payload["environment"])
    raise AssertionError("no environment event was emitted")


def _registry_has_pid(registry: OrphanRegistry, pid: int) -> bool:
    try:
        data = json.loads(registry.path.read_text())
    except FileNotFoundError:
        return False
    return any(entry.get("pid") == pid for entry in data)


def _assert_pid_gone(pid: int) -> None:
    for _ in range(100):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        time.sleep(0.05)
    pytest.fail(f"pid {pid} was still alive")
