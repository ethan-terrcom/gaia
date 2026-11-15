"""CLI tests for ``gaia worktrees cleanup`` (doc 18c)."""

from __future__ import annotations

import argparse
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from gaia import cli
from gaia.config.logging import _reset_for_tests
from gaia.config.settings import Settings
from gaia.db.engine import session_scope
from gaia.db.models import Repository, Run, Ticket, Workspace, Worktree
from gaia.db.repo import ScopedRepo
from gaia.domain.enums import RunStatus
from gaia.services import cleanup as cleanup_module
from gaia.services.cleanup import CleanupPlan, CleanupResult, CleanupSkip
from gaia.services.git import GitService
from gaia.services.worktrees import WorktreeService
from tests.db_builders import make_workspace
from tests.git_repo import on_disk

NOW = datetime(2026, 11, 14, 12, 0, 0, tzinfo=UTC)
OLD = NOW - timedelta(days=30)


@pytest.fixture(autouse=True)
def restore_logging() -> Iterator[None]:
    """``gaia worktrees cleanup`` configures logging; do not leak it."""
    try:
        yield
    finally:
        _reset_for_tests()


@pytest.fixture
def cli_env(
    tmp_path: Path,
    database,
    monkeypatch: pytest.MonkeyPatch,
) -> Settings:
    """Point the CLI at the test database and a temporary data dir."""
    settings = Settings(data_dir=tmp_path / "gaia-data", db_url=database.url)
    monkeypatch.setattr(cli, "get_settings", lambda: settings)
    return settings


async def _seed_world(
    database, git_repo: Path, settings: Settings
) -> tuple[Workspace, Repository, Ticket, GitService, Settings]:
    async with session_scope(database.sessions) as session:
        workspace = await make_workspace(session, slug="local")
        repository = await ScopedRepo(session, Repository, workspace.id).create(
            name="cli-cleanup-repo", local_path=str(git_repo)
        )
        ticket = await ScopedRepo(session, Ticket, workspace.id).create(
            repo_id=repository.id, title="CLI cleanup"
        )
    return workspace, repository, ticket, GitService(settings), settings


async def _make_old_run(
    database,
    git: GitService,
    settings: Settings,
    workspace: Workspace,
    repository: Repository,
    ticket: Ticket,
) -> Path:
    async with session_scope(database.sessions) as session:
        worktree = await WorktreeService(
            git=git, session=session, workspace_id=workspace.id
        ).create(repository, ticket)
        run = await ScopedRepo(session, Run, workspace.id).create(
            ticket_id=ticket.id,
            repo_id=repository.id,
            worktree_id=worktree.id,
        )
        run.status = RunStatus.COMPLETED.value
        run.finished_at = OLD
        await session.flush()
    return Path(worktree.path)


async def test_cli_cleanup_dry_run_removes_nothing(
    database,
    git_repo: Path,
    cli_env: Settings,
    capsys: pytest.CaptureFixture[str],
) -> None:
    workspace, repository, ticket, git, _settings = await _seed_world(database, git_repo, cli_env)
    path = await _make_old_run(database, git, cli_env, workspace, repository, ticket)

    exit_code = await cli._worktrees_cleanup_async(argparse.Namespace(older_than=None, apply=False))

    assert exit_code == 0
    captured = capsys.readouterr()
    assert "Dry run: nothing was removed." in captured.out
    assert on_disk(path).is_dir


async def test_cli_cleanup_apply_removes(
    database,
    git_repo: Path,
    cli_env: Settings,
    capsys: pytest.CaptureFixture[str],
) -> None:
    workspace, repository, ticket, git, _settings = await _seed_world(database, git_repo, cli_env)
    path = await _make_old_run(database, git, cli_env, workspace, repository, ticket)

    exit_code = await cli._worktrees_cleanup_async(argparse.Namespace(older_than=14, apply=True))

    assert exit_code == 0
    captured = capsys.readouterr()
    assert "Removed 1 worktree(s)" in captured.out
    assert not on_disk(path).exists


async def test_cli_cleanup_rejects_nonpositive_older_than(
    cli_env: Settings,
    capsys: pytest.CaptureFixture[str],
) -> None:
    exit_code = await cli._worktrees_cleanup_async(argparse.Namespace(older_than=0, apply=False))

    assert exit_code == 2
    assert "at least 1" in capsys.readouterr().err


def test_print_cleanup_plan_prints_skips_and_scratch(
    capsys: pytest.CaptureFixture[str],
) -> None:
    worktree = Worktree(branch="gaia/keep")
    run = Run(status=RunStatus.COMPLETED.value, finished_at=OLD)
    plan = CleanupPlan(
        worktrees=[],
        scratch_dirs=[Path("/tmp/scratch-run")],
        skipped=[
            CleanupSkip(path=Path("/tmp/no-worktree"), reason="scratch skip"),
            CleanupSkip(
                path=Path("/tmp/keep"),
                reason="uncommitted changes",
                worktree=worktree,
                last_run=run,
            ),
        ],
    )

    cli._print_cleanup_plan(plan)

    captured = capsys.readouterr()
    assert "gaia/keep" in captured.out
    assert "uncommitted changes" in captured.out
    assert "/tmp/scratch-run" in captured.out


def test_fmt_dt_none() -> None:
    assert cli._fmt_dt(None) == ""
    assert cli._fmt_dt(OLD) == OLD.isoformat(timespec="seconds")


class _FakeCleanupService:
    """A CLI test double with an apply result that contains a skip."""

    def __init__(self, *, sessions: object, git: object, settings: object) -> None:
        pass

    async def plan(self, *, older_than_days: int) -> CleanupPlan:
        return CleanupPlan(worktrees=[], scratch_dirs=[], skipped=[])

    async def apply(self, plan: CleanupPlan) -> CleanupResult:
        return CleanupResult(
            worktrees_removed=0,
            scratch_dirs_removed=0,
            skips=[CleanupSkip(path=Path("/tmp/skipped"), reason="boom")],
        )


async def test_cli_cleanup_apply_prints_skips(
    database,
    cli_env: Settings,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(cleanup_module, "CleanupService", _FakeCleanupService)

    exit_code = await cli._worktrees_cleanup_async(argparse.Namespace(older_than=14, apply=True))

    assert exit_code == 0
    captured = capsys.readouterr()
    assert "0 item(s) skipped" not in captured.out
    assert "skipped /tmp/skipped: boom" in captured.out


def test_worktrees_cleanup_runs_the_async_body(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_async(args: argparse.Namespace) -> int:
        return 3

    monkeypatch.setattr(cli, "_worktrees_cleanup_async", fake_async)

    assert cli._worktrees_cleanup(argparse.Namespace()) == 3


def test_worktrees_dispatches_cleanup(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "_worktrees_cleanup", lambda args: 5)

    assert cli._worktrees(argparse.Namespace(worktrees_command="cleanup")) == 5


def test_worktrees_unknown_command() -> None:
    assert cli._worktrees(argparse.Namespace(worktrees_command="wat")) == 2
