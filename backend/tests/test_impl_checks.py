"""Tests for ``SandboxCheckRunner`` (doc 12b-1)."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import pytest

from gaia.config.settings import Settings
from gaia.db.engine import session_scope
from gaia.db.models import Repository, Run, RunStep, Ticket, Workspace, Worktree
from gaia.db.repo import ScopedRepo
from gaia.domain.enums import WorktreeStatus
from gaia.orchestrator.deps import CheckOutcome
from gaia.orchestrator.impl.checks import (
    SandboxCheckRunner,
    _argv_tuple,
    _format_output,
    _step_command,
)
from gaia.security.commands import CommandPolicy
from gaia.services.proc import ProcResult
from tests.db_builders import make_workspace
from tests.git_repo import on_disk, resolved
from tests.tool_fakes import FakeCommandRunner, make_proc_result


def _ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


class _NoneScopedRepo:
    """A stand-in ScopedRepo whose ``get`` always returns ``None``."""

    async def get(self, row_id: str) -> None:
        del row_id
        return None


@dataclass(frozen=True)
class CheckWorld:
    """A run with a step and a worktree path for the check runner."""

    workspace: Workspace
    repository: Repository
    run: Run
    step: RunStep
    worktree: Worktree
    settings: Settings
    runner: FakeCommandRunner
    sessions: Any


@pytest.fixture
async def check_world(database: Any, tmp_path: Path) -> CheckWorld:
    settings = Settings(data_dir=tmp_path / "gaia-data")
    async with session_scope(database.sessions) as session:
        workspace = await make_workspace(session)
        repositories = ScopedRepo(session, Repository, workspace.id)
        repository = await repositories.create(
            name="fixture",
            local_path=str(tmp_path / "repo"),
            test_command=["pytest", "-q"],
        )
        tickets = ScopedRepo(session, Ticket, workspace.id)
        ticket = await tickets.create(repo_id=repository.id, title="Ship it")
        runs = ScopedRepo(session, Run, workspace.id)
        run = await runs.create(
            ticket_id=ticket.id,
            repo_id=repository.id,
            graph_thread_id="thread-check",
            created_by=None,
        )
        worktrees = ScopedRepo(session, Worktree, workspace.id)
        worktree = await worktrees.create(
            id="wt-check",
            repo_id=repository.id,
            path=str(settings.worktrees_root / repository.id / "wt-check"),
            branch="gaia/check",
            status=WorktreeStatus.READY.value,
        )
        worktree_path = Path(worktree.path)
        _ensure_dir(worktree_path)
        await ScopedRepo(session, Run, workspace.id).update(run.id, worktree_id=worktree.id)
        steps = ScopedRepo(session, RunStep, workspace.id)
        step = await steps.create(
            run_id=run.id,
            step_index=0,
            title="Add a file",
            spec_json={"test_command": ["pytest", "-q"]},
        )
    command_runner = FakeCommandRunner()
    return CheckWorld(
        workspace=workspace,
        repository=repository,
        run=run,
        step=step,
        worktree=worktree,
        settings=settings,
        runner=command_runner,
        sessions=database.sessions,
    )


def make_check_runner(world: CheckWorld) -> SandboxCheckRunner:
    return SandboxCheckRunner(
        settings=world.settings,
        sessions=world.sessions,
        commands=world.runner,
        policy=CommandPolicy.from_default(),
    )


async def test_no_test_command_returns_passed_without_running(
    database: Any, tmp_path: Path
) -> None:
    settings = Settings(data_dir=tmp_path / "gaia-data")
    async with session_scope(database.sessions) as session:
        workspace = await make_workspace(session)
        repository = await ScopedRepo(session, Repository, workspace.id).create(
            name="no-tests",
            local_path=str(tmp_path / "repo"),
        )
        ticket = await ScopedRepo(session, Ticket, workspace.id).create(
            repo_id=repository.id, title="Ship it"
        )
        run = await ScopedRepo(session, Run, workspace.id).create(
            ticket_id=ticket.id,
            repo_id=repository.id,
            graph_thread_id="thread-no-check",
            created_by=None,
        )
        worktree = await ScopedRepo(session, Worktree, workspace.id).create(
            id="wt-no-check",
            repo_id=repository.id,
            path=str(settings.worktrees_root / repository.id / "wt-no-check"),
            branch="gaia/no-check",
            status=WorktreeStatus.READY.value,
        )
        _ensure_dir(Path(worktree.path))
        await ScopedRepo(session, Run, workspace.id).update(run.id, worktree_id=worktree.id)
        await ScopedRepo(session, RunStep, workspace.id).create(
            run_id=run.id,
            step_index=0,
            title="No tests",
            spec_json={},
        )
    runner = FakeCommandRunner()
    check_runner = SandboxCheckRunner(
        settings=settings,
        sessions=database.sessions,
        commands=runner,
        policy=CommandPolicy.from_default(),
    )

    outcome = await check_runner.run(run.id, 0)

    assert outcome == CheckOutcome(passed=True, output="no test command configured")
    assert runner.calls == []


async def test_step_test_command_runs_in_scratch_and_persists_summary(
    check_world: CheckWorld,
) -> None:
    check_runner = make_check_runner(check_world)

    outcome = await check_runner.run(check_world.run.id, 0)

    assert outcome.passed is True
    assert "exit=0" in outcome.output
    assert len(check_world.runner.calls) == 1
    argv, cwd, timeout_s, task_dir = check_world.runner.calls[0]
    assert argv == ("pytest", "-q")
    assert cwd == resolved(Path(check_world.worktree.path))
    assert timeout_s == check_world.settings.check_timeout_s
    assert not on_disk(task_dir).exists  # the scratch dir is removed afterwards

    async with session_scope(check_world.sessions) as session:
        step = await ScopedRepo(session, RunStep, check_world.workspace.id).get(check_world.step.id)
        assert step is not None
        assert step.result_summary == "passed: pytest -q"


async def test_failed_command_persists_the_exit_code(check_world: CheckWorld) -> None:
    check_world.runner._results = [
        make_proc_result(
            ("pytest", "-q"),
            cwd=Path(check_world.worktree.path),
            returncode=1,
            stdout="1 failed\n",
            stderr="",
        )
    ]

    outcome = await make_check_runner(check_world).run(check_world.run.id, 0)

    assert outcome.passed is False
    assert "exit=1" in outcome.output
    assert "1 failed" in outcome.output
    async with session_scope(check_world.sessions) as session:
        step = await ScopedRepo(session, RunStep, check_world.workspace.id).get(check_world.step.id)
        assert step is not None
        assert step.result_summary == "failed (exit 1): pytest -q"


async def test_denied_command_is_not_run(check_world: CheckWorld) -> None:
    async with session_scope(check_world.sessions) as session:
        await ScopedRepo(session, RunStep, check_world.workspace.id).update(
            check_world.step.id,
            spec_json={"test_command": ["git", "status"]},
        )

    outcome = await make_check_runner(check_world).run(check_world.run.id, 0)

    assert outcome.passed is False
    assert outcome.output.startswith("check command not allowed: deny: use the git tools (09b)")
    assert check_world.runner.calls == []


async def test_ask_is_treated_as_a_failure_and_never_asks(check_world: CheckWorld) -> None:
    async with session_scope(check_world.sessions) as session:
        await ScopedRepo(session, RunStep, check_world.workspace.id).update(
            check_world.step.id,
            spec_json={"test_command": ["python", "script.py"]},
        )

    outcome = await make_check_runner(check_world).run(check_world.run.id, 0)

    assert outcome.passed is False
    assert outcome.output.startswith("check command not allowed: ask:")
    assert check_world.runner.calls == []


async def test_timeout_is_a_failure(check_world: CheckWorld) -> None:
    check_world.runner._results = [
        make_proc_result(
            ("pytest", "-q"),
            cwd=Path(check_world.worktree.path),
            returncode=-9,
            timed_out=True,
        )
    ]

    outcome = await make_check_runner(check_world).run(check_world.run.id, 0)

    assert outcome.passed is False
    assert "timed out after" in outcome.output


def test_argv_tuple_handles_empty_and_malformed_commands() -> None:
    assert _argv_tuple(None) == ()
    assert _argv_tuple([]) == ()
    assert _argv_tuple(cast(Any, ["ok", 1])) == ()
    assert _argv_tuple(["pytest", "-q"]) == ("pytest", "-q")


def test_format_output_covers_truncated_streams() -> None:
    result = ProcResult(
        argv=("pytest",),
        cwd=Path("/tmp"),
        pid=1,
        returncode=1,
        stdout="out",
        stderr="err",
        stdout_truncated=True,
        stderr_truncated=True,
        duration_s=0.1,
        timed_out=False,
    )

    output = _format_output(result, timeout_s=900)

    assert output.startswith("exit=1")
    assert output.count("[output truncated]") == 2


async def test_check_runner_unknown_run_raises(check_world: CheckWorld) -> None:
    with pytest.raises(ValueError, match="no run"):
        await make_check_runner(check_world).run("missing-run", 0)


async def test_check_runner_missing_step_raises(check_world: CheckWorld) -> None:
    async with session_scope(check_world.sessions) as session:
        tickets = ScopedRepo(session, Ticket, check_world.workspace.id)
        ticket = await tickets.create(repo_id=check_world.repository.id, title="Another ticket")
        run = await ScopedRepo(session, Run, check_world.workspace.id).create(
            ticket_id=ticket.id,
            repo_id=check_world.repository.id,
            graph_thread_id="thread-no-step",
            created_by=None,
        )

    with pytest.raises(ValueError, match="no step"):
        await make_check_runner(check_world).run(run.id, 0)


async def test_check_runner_requires_a_worktree(check_world: CheckWorld) -> None:
    async with session_scope(check_world.sessions) as session:
        tickets = ScopedRepo(session, Ticket, check_world.workspace.id)
        ticket = await tickets.create(repo_id=check_world.repository.id, title="Another ticket")
        run = await ScopedRepo(session, Run, check_world.workspace.id).create(
            ticket_id=ticket.id,
            repo_id=check_world.repository.id,
            graph_thread_id="thread-no-worktree",
            created_by=None,
        )
        await ScopedRepo(session, RunStep, check_world.workspace.id).create(
            run_id=run.id,
            step_index=0,
            title="No worktree",
            spec_json={"test_command": ["pytest", "-q"]},
        )

    with pytest.raises(ValueError, match="has no worktree"):
        await make_check_runner(check_world).run(run.id, 0)


async def test_check_runner_rejects_a_worktree_from_another_repo(
    check_world: CheckWorld,
) -> None:
    async with session_scope(check_world.sessions) as session:
        repositories = ScopedRepo(session, Repository, check_world.workspace.id)
        repo_two = await repositories.create(
            name="repo-two",
            local_path=str(Path(check_world.worktree.path).parent / "repo-two"),
        )
        tickets = ScopedRepo(session, Ticket, check_world.workspace.id)
        ticket_two = await tickets.create(repo_id=repo_two.id, title="Ticket two")
        run = await ScopedRepo(session, Run, check_world.workspace.id).create(
            ticket_id=ticket_two.id,
            repo_id=repo_two.id,
            worktree_id=check_world.worktree.id,
            graph_thread_id="thread-mismatch",
            created_by=None,
        )
        await ScopedRepo(session, RunStep, check_world.workspace.id).create(
            run_id=run.id,
            step_index=0,
            title="Mismatched worktree",
            spec_json={"test_command": ["pytest", "-q"]},
        )

    with pytest.raises(ValueError, match="belongs to repository"):
        await make_check_runner(check_world).run(run.id, 0)


async def test_check_runner_rejects_missing_repository(
    check_world: CheckWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_scoped_repo = ScopedRepo

    def fake_scoped_repo(session: Any, model: Any, workspace_id: str) -> Any:
        if model is Repository:
            return _NoneScopedRepo()
        return real_scoped_repo(session, model, workspace_id)

    monkeypatch.setattr("gaia.orchestrator.impl.checks.ScopedRepo", fake_scoped_repo)

    with pytest.raises(ValueError, match="no repository"):
        await make_check_runner(check_world).run(check_world.run.id, 0)


def test_step_command_falls_back_to_repository_command() -> None:
    repository = Repository(test_command=["pytest", "-q"])
    step = RunStep(
        run_id="run",
        step_index=0,
        title="No dict spec",
        spec_json=cast(Any, []),
    )

    assert _step_command(repository, step) == ("pytest", "-q")


def test_step_command_ignores_a_malformed_step_command() -> None:
    repository = Repository(test_command=["pytest", "-q"])
    step = RunStep(
        run_id="run",
        step_index=0,
        title="Malformed spec",
        spec_json={"test_command": ["ok", 1]},
    )

    assert _step_command(repository, step) == ("pytest", "-q")
