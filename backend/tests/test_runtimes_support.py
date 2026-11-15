"""Unit tests for the runtime base types and shared support functions."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from pydantic import SecretStr

from gaia.domain.enums import AgentRole, EventType
from gaia.runtimes.approvals import InMemoryApprovalBroker
from gaia.runtimes.base import (
    AgentResult,
    AgentTask,
    RuntimeRegistry,
    RuntimeUsage,
    UnknownRuntime,
)
from gaia.runtimes.fake import FakeRuntime, Finish, Sleep
from gaia.runtimes.support import AGENT_PATH, build_agent_env, run_with_limits
from gaia.security import redaction
from gaia.services.git import GitService, WorktreeLocation
from tests.runtime_contract import (
    RecordingSink,
    base_sha,
    make_task,
    make_worktree_location,
    wait_until,
)


def test_build_agent_env_never_reads_os_environ(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-planted-000000000000")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-planted-openai-0000000000")
    monkeypatch.setenv("PATH", "/planted/bin")
    monkeypatch.setenv("HOME", "/planted/home")

    env = build_agent_env(
        home=tmp_path,
        gateway_url=None,
        run_token=None,
        provider=None,
    )

    assert env == {
        "PATH": AGENT_PATH,
        "HOME": str(tmp_path),
        "LANG": "C.UTF-8",
        "TMPDIR": str(tmp_path),
    }
    assert "ANTHROPIC_API_KEY" not in env
    assert "OPENAI_API_KEY" not in env


def test_build_agent_env_accepts_a_custom_path(tmp_path: Path) -> None:
    env = build_agent_env(
        home=tmp_path,
        gateway_url=None,
        run_token=None,
        provider=None,
        path="/opt/homebrew/bin:/usr/bin",
    )

    assert env["PATH"] == "/opt/homebrew/bin:/usr/bin"


def test_build_agent_env_adds_provider_gateway_variables(tmp_path: Path) -> None:
    anthropic = build_agent_env(
        home=tmp_path,
        gateway_url="http://127.0.0.1:8765/llm/anthropic",
        run_token=SecretStr("gaia_rt_test-token"),
        provider="anthropic",
    )
    assert anthropic["ANTHROPIC_BASE_URL"] == "http://127.0.0.1:8765/llm/anthropic"
    assert anthropic["ANTHROPIC_API_KEY"] == "gaia_rt_test-token"

    deepseek = build_agent_env(
        home=tmp_path,
        gateway_url="http://127.0.0.1:8765/llm/deepseek",
        run_token=SecretStr("gaia_rt_test-token"),
        provider="deepseek",
    )
    assert deepseek["OPENAI_BASE_URL"] == "http://127.0.0.1:8765/llm/deepseek"
    assert deepseek["OPENAI_API_KEY"] == "gaia_rt_test-token"


def test_build_agent_env_requires_gateway_parts_when_provider_is_set(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="gateway_url and run_token are required"):
        build_agent_env(home=tmp_path, gateway_url=None, run_token=None, provider="deepseek")


class _StubRuntime:
    name = "stub"

    async def run_task(self, task: AgentTask, events: object) -> AgentResult:
        raise NotImplementedError

    async def cancel(self, task_id: str) -> None:
        return None


def test_runtime_registry_registers_gets_and_lists() -> None:
    registry = RuntimeRegistry()
    runtime = _StubRuntime()

    registry.register(runtime)

    assert registry.get("stub") is runtime
    assert registry.names() == ("stub",)
    with pytest.raises(UnknownRuntime):
        registry.get("missing")
    with pytest.raises(ValueError, match="already registered"):
        registry.register(runtime)


def test_agent_task_defaults(tmp_path: Path) -> None:
    worktree = WorktreeLocation(work_tree=tmp_path, git_dir=tmp_path / ".git")
    task = AgentTask(
        task_id="task-1",
        run_id="run-1",
        run_step_id=None,
        workspace_id="ws-1",
        agent_id=None,
        role=AgentRole.JUNIOR,
        instructions="hello",
        worktree=worktree,
        base_sha="0" * 40,
    )

    assert task.timeout_s == 1800
    assert task.max_turns == 40
    assert task.extra_allow == ()
    assert task.output_schema is None
    usage = RuntimeUsage()
    assert usage.input_tokens == 0
    assert usage.cost_usd == 0


@pytest.fixture
def worktree(git_repo: Path, tmp_path: Path):
    return make_worktree_location(git_repo, tmp_path)


async def test_run_with_limits_turns_failures_into_redacted_failed_results(
    worktree: WorktreeLocation,
    git_repo: Path,
    git_service: GitService,
) -> None:
    secret = "dummy-failure-secret-value"
    redaction.register_secret(secret)
    from gaia.runtimes.fake import Fail

    task = make_task(worktree, base_sha=base_sha(git_repo))
    runtime = FakeRuntime([Fail(f"boom {secret}")], InMemoryApprovalBroker())
    sink = RecordingSink()

    result = await run_with_limits(runtime, task, sink, git=git_service)

    assert result.status == "failed"
    assert "boom" in (result.error or "")
    assert secret not in (result.error or "")


async def test_run_with_limits_re_raises_outer_cancellation(
    worktree: WorktreeLocation,
    git_repo: Path,
    git_service: GitService,
) -> None:
    task = make_task(worktree, base_sha=base_sha(git_repo), timeout_s=60)
    runtime = FakeRuntime([Sleep(30)], InMemoryApprovalBroker())
    sink = RecordingSink()

    run_task = asyncio.create_task(run_with_limits(runtime, task, sink, git=git_service))
    await wait_until(lambda: task.task_id in runtime._cancel_events)
    run_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await run_task


async def test_run_with_limits_uses_git_for_files_changed(
    worktree: WorktreeLocation,
    git_repo: Path,
    git_service: GitService,
) -> None:
    task = make_task(worktree, base_sha=base_sha(git_repo))
    runtime = FakeRuntime(
        [Finish("done", None, files_changed=("claimed.txt",))],
        InMemoryApprovalBroker(),
    )
    sink = RecordingSink()

    result = await run_with_limits(runtime, task, sink, git=git_service)

    assert result.files_changed == ()


class _StubbornRuntime:
    """A runtime whose task never ends and whose cancel never returns."""

    name = "stubborn"

    async def run_task(self, task: Any, events: Any) -> Any:
        await asyncio.Event().wait()

    async def cancel(self, task_id: str) -> None:
        await asyncio.Event().wait()


async def test_run_with_limits_bounds_a_hanging_cancel(
    worktree: WorktreeLocation,
    git_repo: Path,
    git_service: GitService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Lead review 10a (S1): a runtime that never finishes cancelling cannot defeat the timeout."""
    monkeypatch.setattr("gaia.runtimes.support.CANCEL_GRACE_S", 0.05)
    task = make_task(worktree, base_sha=base_sha(git_repo), timeout_s=0.05)

    result = await asyncio.wait_for(
        run_with_limits(_StubbornRuntime(), task, RecordingSink(), git=git_service), timeout=5
    )

    assert result.status == "timeout"


async def test_run_with_limits_starts_with_an_agent_message_in_the_shared_shape(
    worktree: WorktreeLocation,
    git_repo: Path,
    git_service: GitService,
) -> None:
    """Every ``agent.message`` carries its text under ``content`` (17c review, R3)."""
    task = make_task(worktree, base_sha=base_sha(git_repo))
    runtime = FakeRuntime([Finish("done", None)], InMemoryApprovalBroker())
    sink = RecordingSink()

    await run_with_limits(runtime, task, sink, git=git_service)

    first_type, first_payload = sink.events[0]
    assert first_type is EventType.AGENT_MESSAGE
    assert first_payload == {"role": task.role.value, "content": task.instructions}
