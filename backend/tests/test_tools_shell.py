"""Tests for the command tool and the srt command runner (doc 09b)."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any, cast

import pytest

from gaia.config.settings import Settings
from gaia.runtimes.approvals import InMemoryApprovalBroker
from gaia.tools import shell
from gaia.tools.registry import ToolRegistry
from tests.runtime_contract import wait_for_pending
from tests.tool_fakes import FakeCommandRunner, make_proc_result, make_tool_context


def _resolved(path: Path) -> Path:
    return path.resolve()


@pytest.fixture
def worktree(tmp_path: Path) -> Path:
    path = tmp_path / "worktree"
    path.mkdir()
    return path


@pytest.fixture
def registry() -> ToolRegistry:
    built = ToolRegistry()
    built.register(shell.run_command)
    return built


async def test_git_push_is_denied_without_running(registry: ToolRegistry, worktree: Path) -> None:
    runner = FakeCommandRunner()
    ctx = make_tool_context(worktree, commands=runner)

    result = await registry.call(
        "run_command", {"argv": ["git", "push"]}, ctx, call_id="call-git-push"
    )

    assert result.ok is False
    assert result.decision == "deny"
    assert result.content.startswith("denied by policy: deny: use the git tools")
    assert runner.calls == []


async def test_pytest_is_allowed_and_runs(registry: ToolRegistry, worktree: Path) -> None:
    runner = FakeCommandRunner(
        [
            make_proc_result(
                ["pytest", "-k", "x"],
                cwd=worktree,
                stdout="1 passed\n",
            )
        ]
    )
    ctx = make_tool_context(worktree, commands=runner)

    result = await registry.call(
        "run_command",
        {"argv": ["pytest", "-k", "x"]},
        ctx,
        call_id="call-pytest",
    )

    assert result.ok is True
    assert result.decision == "allow"
    assert result.content.startswith("exit=0\n--- stdout ---\n1 passed\n")
    assert runner.calls == [(("pytest", "-k", "x"), _resolved(worktree), 300.0, ctx.task_dir)]


async def test_python_script_asks_and_approved_runs(registry: ToolRegistry, worktree: Path) -> None:
    runner = FakeCommandRunner(
        [
            make_proc_result(
                ["python", "script.py"],
                cwd=worktree,
                stdout="ran\n",
            )
        ]
    )
    ctx = make_tool_context(worktree, commands=runner)

    broker = cast(InMemoryApprovalBroker, ctx.approvals)
    request = asyncio.create_task(
        registry.call(
            "run_command",
            {"argv": ["python", "script.py"]},
            ctx,
            call_id="call-python-approved",
        )
    )
    approval_id = await wait_for_pending(broker)
    broker.decide(approval_id, True, "ok")

    result = await asyncio.wait_for(request, timeout=5)

    assert result.ok is True
    assert result.decision == "ask_approved"
    assert runner.calls == [(("python", "script.py"), _resolved(worktree), 300.0, ctx.task_dir)]


async def test_python_script_rejected_does_not_run(registry: ToolRegistry, worktree: Path) -> None:
    runner = FakeCommandRunner()
    ctx = make_tool_context(worktree, commands=runner)

    broker = cast(InMemoryApprovalBroker, ctx.approvals)
    request = asyncio.create_task(
        registry.call(
            "run_command",
            {"argv": ["python", "script.py"]},
            ctx,
            call_id="call-python-rejected",
        )
    )
    approval_id = await wait_for_pending(broker)
    broker.decide(approval_id, False, "no")

    result = await asyncio.wait_for(request, timeout=5)

    assert result.ok is False
    assert result.decision == "ask_rejected"
    assert result.content == "not approved: no"
    assert runner.calls == []


async def test_extra_allow_lets_the_owner_command_through(
    registry: ToolRegistry, worktree: Path
) -> None:
    runner = FakeCommandRunner([make_proc_result(["npm", "test"], cwd=worktree, stdout="ok\n")])
    ctx = make_tool_context(worktree, commands=runner, extra_allow=(("npm", "test"),))

    result = await registry.call(
        "run_command", {"argv": ["npm", "test"]}, ctx, call_id="call-extra"
    )

    assert result.ok is True
    assert result.decision == "allow"
    assert runner.calls == [(("npm", "test"), _resolved(worktree), 300.0, ctx.task_dir)]


async def test_timeout_content(registry: ToolRegistry, worktree: Path) -> None:
    runner = FakeCommandRunner(
        [
            make_proc_result(
                ["pytest"],
                cwd=worktree,
                returncode=-9,
                timed_out=True,
            )
        ]
    )
    ctx = make_tool_context(worktree, commands=runner)

    result = await registry.call(
        "run_command",
        {"argv": ["pytest"], "timeout_s": 7},
        ctx,
        call_id="call-timeout",
    )

    assert result.ok is False
    assert result.content.startswith("timed out after 7s\n--- stdout ---")


async def test_output_truncation_notes(registry: ToolRegistry, worktree: Path) -> None:
    runner = FakeCommandRunner(
        [
            make_proc_result(
                ["pytest"],
                cwd=worktree,
                stdout="stdout",
                stderr="stderr",
                stdout_truncated=True,
                stderr_truncated=True,
            )
        ]
    )
    ctx = make_tool_context(worktree, commands=runner)

    result = await registry.call("run_command", {"argv": ["pytest"]}, ctx, call_id="call-truncated")

    assert result.ok is True
    assert result.content.count("[output truncated]") == 2


def test_command_env_never_reads_the_parent_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-planted-0000000000000000000000")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-planted-openai-0000000000")
    monkeypatch.setenv("PATH", "/planted/bin")
    monkeypatch.setenv("HOME", "/planted/home")
    settings = Settings(
        data_dir=tmp_path / "data",
        agent_path=("/opt/agent/bin", "/usr/bin"),
    )
    task_dir = tmp_path / "task"

    env = shell.command_env(settings, task_dir)

    assert env["PATH"] == "/opt/agent/bin:/usr/bin"
    assert env["HOME"] == str(task_dir)
    assert env["TMPDIR"] == str(task_dir)
    assert env["UV_NO_SYNC"] == "1"
    assert env["UV_CACHE_DIR"] == str(task_dir / "uv-cache")
    assert env["PYTHONDONTWRITEBYTECODE"] == "1"
    assert "ANTHROPIC_API_KEY" not in env
    assert "OPENAI_API_KEY" not in env


def test_command_env_points_uv_at_the_readable_python_install_dir(tmp_path: Path) -> None:
    """Setup's venv must link to a Python that later sandboxes can read (live bug, 2026-11-15)."""
    toolchain = tmp_path / "uv"
    pythons = toolchain / "python"
    pythons.mkdir(parents=True)
    settings = Settings(
        data_dir=tmp_path / "data",
        sandbox_toolchain_read=(toolchain,),
        sandbox_uv_python_dir=pythons,
    )

    env = shell.command_env(settings, tmp_path / "task")

    assert env["UV_PYTHON_INSTALL_DIR"] == str(pythons)


@pytest.mark.parametrize("case", ["missing", "unreadable", "disabled"])
def test_command_env_leaves_uv_alone_without_a_usable_python_dir(tmp_path: Path, case: str) -> None:
    toolchain = tmp_path / "uv"
    pythons = toolchain / "python"
    if case != "missing":
        pythons.mkdir(parents=True)
    settings = Settings(
        data_dir=tmp_path / "data",
        # "unreadable": the directory exists but no toolchain entry covers it, so
        # the sandbox would deny it and uv's own default is the better choice.
        sandbox_toolchain_read=(tmp_path / "elsewhere",) if case == "unreadable" else (toolchain,),
        sandbox_uv_python_dir=None if case == "disabled" else pythons,
    )

    env = shell.command_env(settings, tmp_path / "task")

    assert "UV_PYTHON_INSTALL_DIR" not in env


def test_settings_expand_the_uv_python_dir() -> None:
    assert (
        Settings(sandbox_uv_python_dir=Path("~/x")).sandbox_uv_python_dir
        == Path("~/x").expanduser()
    )


async def test_srt_command_runner_builds_the_wrapped_argv_and_private_settings(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    task_dir = tmp_path / "task"
    task_dir.mkdir()
    srt_path = tmp_path / "srt"
    srt_path.write_text("#!/bin/sh\n", encoding="utf-8")
    srt_path.chmod(0o755)
    settings = Settings(
        data_dir=tmp_path / "data",
        agent_path=("/opt/agent/bin", "/usr/bin"),
    )
    fake_result = make_proc_result(["pytest", "-q"], cwd=worktree)
    calls: dict[str, Any] = {}

    async def fake_run_proc(argv, cwd, timeout_s, env, max_output_bytes):
        calls["argv"] = list(argv)
        calls["cwd"] = cwd
        calls["timeout_s"] = timeout_s
        calls["env"] = env
        calls["max_output_bytes"] = max_output_bytes
        settings_file = Path(argv[2])
        calls["mode"] = os.stat(settings_file).st_mode & 0o777
        calls["dir_mode"] = os.stat(settings_file.parent).st_mode & 0o777
        return fake_result

    monkeypatch.setattr(shell, "run_proc", fake_run_proc)

    result = await shell.SrtCommandRunner(settings, srt_path=srt_path).run(
        ["pytest", "-q"],
        cwd=worktree,
        timeout_s=30,
        task_dir=task_dir,
    )

    assert result is fake_result
    settings_path = Path(calls["argv"][2])
    assert calls["argv"] == [
        str(srt_path),
        "--settings",
        str(settings_path),
        "--",
        "/usr/bin/env",
        "-u",
        "NO_PROXY",
        "-u",
        "no_proxy",
        "pytest",
        "-q",
    ]
    assert calls["cwd"] == worktree
    assert calls["timeout_s"] == 30
    assert calls["max_output_bytes"] == 64_000
    assert calls["env"]["PATH"] == "/opt/agent/bin:/usr/bin"
    assert calls["mode"] == 0o600
    assert calls["dir_mode"] == 0o700
    # Lead review S1: outside every sandbox-writable path, and gone afterwards.
    assert settings_path.is_relative_to(tmp_path / "data" / "sandbox")
    assert not settings_path.is_relative_to(task_dir)
    assert not settings_path.is_relative_to(worktree)
    assert not settings_path.parent.exists()
