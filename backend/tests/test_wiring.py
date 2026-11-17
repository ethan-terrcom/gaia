"""Composition-root tests: one broker, both runtimes, and lazy srt lookup."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from gaia.config.secrets import InMemorySecretStore
from gaia.config.settings import Settings
from gaia.domain.enums import AgentRole, RuntimeKind
from gaia.orchestrator.auto_mode import AutoModeState
from gaia.orchestrator.wiring import LazySrtRunner, build_wiring
from gaia.runtimes.base import AgentTask
from gaia.services.costs import load_price_table
from gaia.services.events import EventBus
from gaia.services.git import GitService, WorktreeLocation
from tests.tool_fakes import FakeCommandRunner, RecordingEventSink


async def test_build_wiring_registers_both_runtimes_and_shares_one_broker(
    database: Any,
    tmp_path: Path,
    git_service: GitService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings(data_dir=tmp_path / "gaia-data")

    def _fail_find_srt(settings: Settings) -> Path:
        del settings
        raise AssertionError("find_srt must not be called while building wiring")

    monkeypatch.setattr("gaia.orchestrator.wiring.find_srt", _fail_find_srt)

    wiring = build_wiring(
        settings=settings,
        sessions=database.sessions,
        bus=EventBus(database.sessions),
        git=git_service,
        secrets=InMemorySecretStore(),
        prices=load_price_table(),
    )

    assert wiring.runtimes.names() == (
        RuntimeKind.CLAUDE_AGENT_SDK.value,
        RuntimeKind.NATIVE_DEEPSEEK.value,
    )
    assert wiring.runtimes.get(settings.junior_runtime.value).name == settings.junior_runtime.value
    assert wiring.runtimes.get(settings.lead_runtime.value).name == settings.lead_runtime.value

    task = AgentTask(
        task_id="task-wiring",
        run_id="run-wiring",
        run_step_id=None,
        workspace_id="ws-wiring",
        agent_id=None,
        role=AgentRole.JUNIOR,
        instructions="use tools",
        worktree=WorktreeLocation(
            work_tree=tmp_path / "worktree",
            git_dir=tmp_path / "worktree" / ".git",
        ),
        base_sha="0" * 40,
    )
    events = RecordingEventSink()
    native: Any = wiring.runtimes.get(RuntimeKind.NATIVE_DEEPSEEK.value)
    claude: Any = wiring.runtimes.get(RuntimeKind.CLAUDE_AGENT_SDK.value)

    native_ctx = await native._context_factory(task, events)
    claude_ctx = await claude._context_factory(task, events, tmp_path / "sdk-task-dir")

    assert native_ctx.approvals is wiring.approvals
    assert claude_ctx.approvals is wiring.approvals
    assert native_ctx.audit is claude_ctx.audit
    assert native_ctx.commands is claude_ctx.commands

    # Auto mode follows each runtime's model family and is read live.
    assert native_ctx.auto_approve() is False
    assert claude_ctx.auto_approve() is False
    wiring.auto_mode.write(AutoModeState(deepseek=True))
    assert native_ctx.auto_approve() is True
    assert claude_ctx.auto_approve() is False
    wiring.auto_mode.write(AutoModeState(claude=True))
    assert native_ctx.auto_approve() is False
    assert claude_ctx.auto_approve() is True


async def test_build_wiring_rejects_a_bad_rg_path(
    database: Any,
    tmp_path: Path,
    git_service: GitService,
) -> None:
    settings = Settings(
        data_dir=tmp_path / "gaia-data",
        rg_path=tmp_path / "missing-rg",
    )

    with pytest.raises(ValueError, match="invalid rg_path setting"):
        build_wiring(
            settings=settings,
            sessions=database.sessions,
            bus=EventBus(database.sessions),
            git=git_service,
            secrets=InMemorySecretStore(),
            prices=load_price_table(),
        )


async def test_lazy_srt_runner_builds_once_and_delegates(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings(data_dir=tmp_path / "gaia-data")

    find_calls: list[str] = []
    built_runners: list[FakeCommandRunner] = []

    def fake_find_srt(settings: Settings) -> Path:
        del settings
        find_calls.append("find_srt")
        return tmp_path / "srt"

    class FakeSrtRunner:
        def __init__(
            self,
            settings: Settings,
            *,
            srt_path: Path,
            allowed_domains: tuple[str, ...] = (),
        ) -> None:
            del settings
            self.srt_path = srt_path
            self.allowed_domains = allowed_domains
            self.runner = FakeCommandRunner()
            built_runners.append(self.runner)

        async def run(
            self,
            argv: Any,
            *,
            cwd: Path,
            timeout_s: float,
            task_dir: Path,
        ) -> Any:
            return await self.runner.run(
                argv,
                cwd=cwd,
                timeout_s=timeout_s,
                task_dir=task_dir,
            )

    monkeypatch.setattr("gaia.orchestrator.wiring.find_srt", fake_find_srt)
    monkeypatch.setattr("gaia.orchestrator.wiring.SrtCommandRunner", FakeSrtRunner)

    runner = LazySrtRunner(settings, allowed_domains=("pypi.org",))
    first = await runner.run(
        ["pytest", "-q"],
        cwd=tmp_path,
        timeout_s=10,
        task_dir=tmp_path / "task",
    )
    second = await runner.run(
        ["pytest", "-q"],
        cwd=tmp_path,
        timeout_s=10,
        task_dir=tmp_path / "task",
    )

    assert find_calls == ["find_srt"]
    assert len(built_runners) == 1
    assert built_runners[0].calls[0][0] == ("pytest", "-q")
    assert first.returncode == 0
    assert second.returncode == 0
