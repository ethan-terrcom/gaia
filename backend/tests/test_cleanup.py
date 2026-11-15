"""Doc 18c ``CleanupService``: plan, apply and the background round."""

from __future__ import annotations

import asyncio
import shutil
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from gaia.api import app as app_module
from gaia.api.app import create_app
from gaia.config.secrets import InMemorySecretStore
from gaia.config.settings import Settings
from gaia.db.engine import session_scope
from gaia.db.models import AuditLog, PullRequest, Repository, Run, Ticket, Workspace, Worktree
from gaia.db.repo import AuditLogRepo, ScopedRepo
from gaia.domain.enums import PullRequestState, RunStatus, WorktreeStatus
from gaia.security.paths import UnsafePath
from gaia.services.cleanup import (
    CleanupItem,
    CleanupPlan,
    CleanupResult,
    CleanupService,
    CleanupSkip,
)
from gaia.services.git import GitError, GitService, WorktreeLocation
from gaia.services.proc import ProcResult
from gaia.services.worktrees import WorktreeService
from tests.conftest import Database
from tests.db_builders import make_workspace
from tests.git_repo import on_disk, resolved

NOW = datetime(2026, 11, 14, 12, 0, 0, tzinfo=UTC)
OLD = NOW - timedelta(days=30)
RECENT = NOW - timedelta(days=2)


@dataclass(frozen=True)
class CleanupWorld:
    """One workspace, one repository row pointing at a real git repo, one ticket."""

    database: Database
    workspace: Workspace
    repository: Repository
    ticket: Ticket
    settings: Settings
    git: GitService


@pytest.fixture
async def world(
    database: Database,
    git_repo: Path,
    git_settings: Settings,
    git_service: GitService,
) -> CleanupWorld:
    """A workspace whose single repository is the ``git_repo`` fixture."""
    async with session_scope(database.sessions) as session:
        workspace = await make_workspace(session, slug="cleanup")
        repository = await ScopedRepo(session, Repository, workspace.id).create(
            name="cleanup-repo", local_path=str(git_repo)
        )
        ticket = await ScopedRepo(session, Ticket, workspace.id).create(
            repo_id=repository.id, title="Cleanup me"
        )
    return CleanupWorld(
        database=database,
        workspace=workspace,
        repository=repository,
        ticket=ticket,
        settings=git_settings,
        git=git_service,
    )


def _service(world: CleanupWorld, *, now: datetime = NOW) -> CleanupService:
    """Build the cleanup service under test, with a fixed clock."""
    return CleanupService(
        sessions=world.database.sessions,
        git=world.git,
        settings=world.settings,
        now=lambda: now,
    )


async def _make_worktree(world: CleanupWorld) -> Worktree:
    """Create a real git worktree through the real service."""
    async with session_scope(world.database.sessions) as session:
        service = WorktreeService(git=world.git, session=session, workspace_id=world.workspace.id)
        return await service.create(world.repository, world.ticket)


async def _location(world: CleanupWorld, worktree: Worktree) -> WorktreeLocation:
    """Return the pinned location for ``worktree``."""
    async with session_scope(world.database.sessions) as session:
        service = WorktreeService(git=world.git, session=session, workspace_id=world.workspace.id)
        return await service.location(worktree)


async def _add_run(
    world: CleanupWorld,
    worktree: Worktree | None,
    *,
    status: RunStatus,
    finished_at: datetime | None,
) -> Run:
    """Create a run row in the world's workspace."""
    async with session_scope(world.database.sessions) as session:
        run = await ScopedRepo(session, Run, world.workspace.id).create(
            ticket_id=world.ticket.id,
            repo_id=world.repository.id,
            worktree_id=worktree.id if worktree is not None else None,
        )
        run.status = status.value
        run.finished_at = finished_at
        await session.flush()
        return run


async def _add_merged_pr(world: CleanupWorld, run: Run) -> PullRequest:
    """Create a merged PR row for ``run``."""
    async with session_scope(world.database.sessions) as session:
        return await ScopedRepo(session, PullRequest, world.workspace.id).create(
            run_id=run.id,
            number=1,
            url="https://github.com/acme/widgets/pull/1",
            state=PullRequestState.MERGED.value,
        )


async def _audit_rows(world: CleanupWorld) -> list[AuditLog]:
    """Return the workspace's audit log rows, oldest first."""
    async with session_scope(world.database.sessions) as session:
        return await AuditLogRepo(session, AuditLog, world.workspace.id).list()


def _make_symlink(target: Path, link: Path) -> None:
    """``Path.symlink_to`` outside a coroutine (ruff ASYNC240)."""
    link.symlink_to(target, target_is_directory=True)


def _git_error() -> GitError:
    """A :class:`GitError` shaped like a failed ``git log``."""
    return GitError(
        ("log",),
        ProcResult(
            argv=("git", "log"),
            cwd=Path("/tmp"),
            pid=1,
            returncode=128,
            stdout="",
            stderr="fatal: bad revision\n",
            stdout_truncated=False,
            stderr_truncated=False,
            duration_s=0.01,
            timed_out=False,
        ),
    )


# ------------------------------------------------------------ candidate selection --


async def test_old_completed_run_is_candidate(world: CleanupWorld) -> None:
    worktree = await _make_worktree(world)
    run = await _add_run(world, worktree, status=RunStatus.COMPLETED, finished_at=OLD)

    plan = await _service(world).plan(older_than_days=14)

    assert plan.scratch_dirs == []
    assert plan.skipped == []
    assert len(plan.worktrees) == 1
    item = plan.worktrees[0]
    assert item.worktree.id == worktree.id
    assert item.path == resolved(Path(worktree.path))
    assert item.last_run.id == run.id


async def test_recent_run_is_skipped_and_not_listed(world: CleanupWorld) -> None:
    worktree = await _make_worktree(world)
    await _add_run(world, worktree, status=RunStatus.COMPLETED, finished_at=RECENT)

    plan = await _service(world).plan(older_than_days=14)

    assert plan.worktrees == []
    assert [skip.reason for skip in plan.skipped] == ["not old enough"]
    assert plan.skipped[0].worktree is not None
    assert plan.skipped[0].worktree.id == worktree.id


async def test_an_active_run_on_the_worktree_blocks_cleanup(world: CleanupWorld) -> None:
    worktree = await _make_worktree(world)
    await _add_run(world, worktree, status=RunStatus.COMPLETED, finished_at=OLD)
    await _add_run(world, worktree, status=RunStatus.IMPLEMENTING, finished_at=None)

    plan = await _service(world).plan(older_than_days=14)

    assert plan.worktrees == []
    assert [skip.reason for skip in plan.skipped] == ["active run"]


async def test_terminal_run_with_null_finished_at_is_skipped(world: CleanupWorld) -> None:
    worktree = await _make_worktree(world)
    await _add_run(world, worktree, status=RunStatus.COMPLETED, finished_at=None)

    plan = await _service(world).plan(older_than_days=14)

    assert plan.worktrees == []
    assert [skip.reason for skip in plan.skipped] == ["no finish time"]


@pytest.mark.parametrize("status", [RunStatus.FAILED, RunStatus.CANCELLED])
async def test_failed_and_cancelled_runs_are_terminal(
    world: CleanupWorld, status: RunStatus
) -> None:
    worktree = await _make_worktree(world)
    await _add_run(world, worktree, status=status, finished_at=OLD)

    plan = await _service(world).plan(older_than_days=14)

    assert len(plan.worktrees) == 1


async def test_a_live_worktree_with_no_runs_is_skipped(world: CleanupWorld) -> None:
    await _make_worktree(world)

    plan = await _service(world).plan(older_than_days=14)

    assert plan.worktrees == []
    assert [skip.reason for skip in plan.skipped] == ["no runs"]


async def test_older_than_days_must_be_positive(world: CleanupWorld) -> None:
    with pytest.raises(ValueError, match="positive"):
        await _service(world).plan(older_than_days=0)


# --------------------------------------------------------------------- dirty --


async def test_plan_skips_a_dirty_worktree(world: CleanupWorld) -> None:
    worktree = await _make_worktree(world)
    await _add_run(world, worktree, status=RunStatus.COMPLETED, finished_at=OLD)
    precious = Path(worktree.path) / "precious.txt"
    precious.write_text("keep me\n", encoding="utf-8")

    plan = await _service(world).plan(older_than_days=14)

    assert plan.worktrees == []
    assert [skip.reason for skip in plan.skipped] == ["uncommitted changes"]
    assert precious.read_text(encoding="utf-8") == "keep me\n"


async def test_apply_skips_a_worktree_dirtied_between_plan_and_apply(
    world: CleanupWorld,
) -> None:
    worktree = await _make_worktree(world)
    await _add_run(world, worktree, status=RunStatus.COMPLETED, finished_at=OLD)
    service = _service(world)
    plan = await service.plan(older_than_days=14)
    assert len(plan.worktrees) == 1

    precious = Path(worktree.path) / "precious.txt"
    precious.write_text("keep me\n", encoding="utf-8")

    result = await service.apply(plan)

    assert result.worktrees_removed == 0
    assert result.scratch_dirs_removed == 0
    assert [skip.reason for skip in result.skips] == ["uncommitted changes"]
    assert precious.read_text(encoding="utf-8") == "keep me\n"
    assert on_disk(Path(worktree.path)).is_dir


async def test_apply_skips_a_worktree_tampered_between_plan_and_apply(
    world: CleanupWorld,
) -> None:
    worktree = await _make_worktree(world)
    await _add_run(world, worktree, status=RunStatus.COMPLETED, finished_at=OLD)
    service = _service(world)
    plan = await service.plan(older_than_days=14)
    assert len(plan.worktrees) == 1

    (Path(worktree.path) / ".git").write_text("gitdir: /nonexistent\n", encoding="utf-8")

    result = await service.apply(plan)

    assert result.worktrees_removed == 0
    assert [skip.reason for skip in result.skips] == ["tampered worktree"]
    assert on_disk(Path(worktree.path)).is_dir


# --------------------------------------------------------------- unmerged work --


async def test_unmerged_commits_are_skipped(world: CleanupWorld) -> None:
    worktree = await _make_worktree(world)
    await _add_run(world, worktree, status=RunStatus.COMPLETED, finished_at=OLD)
    location = await _location(world, worktree)
    (Path(worktree.path) / "agent.txt").write_text("agent work\n", encoding="utf-8")
    await world.git.commit(location, "agent work")

    plan = await _service(world).plan(older_than_days=14)

    assert plan.worktrees == []
    assert [skip.reason for skip in plan.skipped] == ["unmerged commits"]


async def test_unmerged_commits_are_allowed_once_the_pr_is_merged(
    world: CleanupWorld,
) -> None:
    worktree = await _make_worktree(world)
    run = await _add_run(world, worktree, status=RunStatus.COMPLETED, finished_at=OLD)
    await _add_merged_pr(world, run)
    location = await _location(world, worktree)
    (Path(worktree.path) / "agent.txt").write_text("agent work\n", encoding="utf-8")
    await world.git.commit(location, "agent work")

    plan = await _service(world).plan(older_than_days=14)

    assert len(plan.worktrees) == 1


async def test_unmerged_check_failure_is_skipped(
    world: CleanupWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    worktree = await _make_worktree(world)
    await _add_run(world, worktree, status=RunStatus.COMPLETED, finished_at=OLD)

    async def explode(*args: object, **kwargs: object) -> list[object]:
        raise _git_error()

    monkeypatch.setattr(world.git, "log", explode)

    plan = await _service(world).plan(older_than_days=14)

    assert plan.worktrees == []
    assert [skip.reason for skip in plan.skipped] == ["unmerged check failed"]


async def test_missing_base_sha_is_skipped(world: CleanupWorld) -> None:
    worktree = await _make_worktree(world)
    await _add_run(world, worktree, status=RunStatus.COMPLETED, finished_at=OLD)
    async with session_scope(world.database.sessions) as session:
        await ScopedRepo(session, Worktree, world.workspace.id).update(worktree.id, base_sha=None)

    plan = await _service(world).plan(older_than_days=14)

    assert plan.worktrees == []
    assert [skip.reason for skip in plan.skipped] == ["missing base sha"]


# --------------------------------------------------------------- path refusals --


async def _make_row_worktree(
    world: CleanupWorld, *, path: Path, status: str = WorktreeStatus.READY.value
) -> Worktree:
    """Insert a worktree row without creating a real worktree."""
    async with session_scope(world.database.sessions) as session:
        return await ScopedRepo(session, Worktree, world.workspace.id).create(
            repo_id=world.repository.id,
            path=str(path),
            branch="gaia/row",
            base_sha="0" * 40,
            status=status,
        )


async def test_unsafe_worktree_path_is_skipped(world: CleanupWorld, tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    worktree = await _make_row_worktree(world, path=outside)
    await _add_run(world, worktree, status=RunStatus.COMPLETED, finished_at=OLD)

    plan = await _service(world).plan(older_than_days=14)

    assert plan.worktrees == []
    assert [skip.reason for skip in plan.skipped] == ["unsafe path"]


async def test_missing_worktree_directory_is_skipped(world: CleanupWorld) -> None:
    path = world.settings.worktrees_root / "missing-row"
    worktree = await _make_row_worktree(world, path=path)
    await _add_run(world, worktree, status=RunStatus.COMPLETED, finished_at=OLD)

    plan = await _service(world).plan(older_than_days=14)

    assert plan.worktrees == []
    assert [skip.reason for skip in plan.skipped] == ["directory missing"]


async def test_symlink_worktree_is_skipped(world: CleanupWorld, tmp_path: Path) -> None:
    worktree = await _make_worktree(world)
    await _add_run(world, worktree, status=RunStatus.COMPLETED, finished_at=OLD)
    row_path = Path(worktree.path)
    target = tmp_path / "symlink-target"
    target.mkdir()
    shutil.rmtree(row_path)
    _make_symlink(target, row_path)

    plan = await _service(world).plan(older_than_days=14)

    assert plan.worktrees == []
    assert [skip.reason for skip in plan.skipped] == ["symlink worktree"]
    assert target.is_dir()


async def test_missing_git_dir_is_skipped(world: CleanupWorld) -> None:
    path = world.settings.worktrees_root / "no-git-dir"
    path.mkdir(parents=True)
    worktree = await _make_row_worktree(world, path=path)
    await _add_run(world, worktree, status=RunStatus.COMPLETED, finished_at=OLD)

    plan = await _service(world).plan(older_than_days=14)

    assert plan.worktrees == []
    assert [skip.reason for skip in plan.skipped] == ["missing git dir"]


async def test_tampered_worktree_is_skipped(world: CleanupWorld) -> None:
    worktree = await _make_worktree(world)
    await _add_run(world, worktree, status=RunStatus.COMPLETED, finished_at=OLD)
    (Path(worktree.path) / ".git").write_text("gitdir: /nonexistent\n", encoding="utf-8")

    plan = await _service(world).plan(older_than_days=14)

    assert plan.worktrees == []
    assert [skip.reason for skip in plan.skipped] == ["tampered worktree"]


# --------------------------------------------------------------------- scratch --


async def _add_run_without_worktree(
    world: CleanupWorld, *, status: RunStatus, finished_at: datetime | None
) -> Run:
    return await _add_run(world, None, status=status, finished_at=finished_at)


async def test_scratch_dir_is_removed_for_an_old_terminal_run(world: CleanupWorld) -> None:
    run = await _add_run_without_worktree(world, status=RunStatus.COMPLETED, finished_at=OLD)
    scratch = world.settings.data_dir / "runs" / run.id / "setup-tmp"
    scratch.mkdir(parents=True)

    plan = await _service(world).plan(older_than_days=14)

    assert plan.scratch_dirs == [resolved(world.settings.data_dir / "runs" / run.id)]


async def test_scratch_symlink_is_not_followed(world: CleanupWorld, tmp_path: Path) -> None:
    run = await _add_run_without_worktree(world, status=RunStatus.COMPLETED, finished_at=OLD)
    runs_root = world.settings.data_dir / "runs"
    runs_root.mkdir(parents=True)
    target = tmp_path / "scratch-target"
    target.mkdir()
    (target / "keep.txt").write_text("keep\n", encoding="utf-8")
    _make_symlink(target, runs_root / run.id)

    plan = await _service(world).plan(older_than_days=14)

    assert plan.scratch_dirs == []
    assert (target / "keep.txt").read_text(encoding="utf-8") == "keep\n"


async def test_scratch_name_that_is_not_a_run_id_is_ignored(world: CleanupWorld) -> None:
    runs_root = world.settings.data_dir / "runs"
    (runs_root / "not-a-run-id").mkdir(parents=True)

    plan = await _service(world).plan(older_than_days=14)

    assert plan.scratch_dirs == []


# ----------------------------------------------------------------------- apply --


async def test_apply_removes_worktree_and_writes_audit(world: CleanupWorld) -> None:
    worktree = await _make_worktree(world)
    await _add_run(world, worktree, status=RunStatus.COMPLETED, finished_at=OLD)
    service = _service(world)
    plan = await service.plan(older_than_days=14)

    result = await service.apply(plan)

    assert result.worktrees_removed == 1
    assert result.scratch_dirs_removed == 0
    assert result.skips == []
    assert not on_disk(Path(worktree.path)).exists
    audit = await _audit_rows(world)
    assert [row.action for row in audit] == ["cleanup.worktree"]
    assert [row.actor for row in audit] == ["system"]
    assert audit[0].target == str(plan.worktrees[0].path)


async def test_apply_removes_scratch_and_writes_audit(world: CleanupWorld) -> None:
    run = await _add_run_without_worktree(world, status=RunStatus.CANCELLED, finished_at=OLD)
    scratch = world.settings.data_dir / "runs" / run.id
    scratch.mkdir(parents=True)
    service = _service(world)
    plan = await service.plan(older_than_days=14)

    result = await service.apply(plan)

    assert result.worktrees_removed == 0
    assert result.scratch_dirs_removed == 1
    assert result.skips == []
    assert not on_disk(scratch).exists
    audit = await _audit_rows(world)
    assert [row.action for row in audit] == ["cleanup.scratch"]
    assert [row.actor for row in audit] == ["system"]


async def test_apply_worktree_unsafe_path_is_skipped(
    world: CleanupWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    worktree = await _make_worktree(world)
    run = await _add_run(world, worktree, status=RunStatus.COMPLETED, finished_at=OLD)
    item = CleanupItem(worktree=worktree, path=resolved(Path(worktree.path)), last_run=run)

    async def explode(self: object, worktree: object, *, force: bool) -> Worktree:
        raise UnsafePath("outside worktrees root")

    monkeypatch.setattr(WorktreeService, "remove", explode)
    skip = await _service(world)._apply_worktree(item)

    assert skip is not None
    assert skip.reason == "unsafe path"


async def test_apply_scratch_symlink_is_skipped(world: CleanupWorld, tmp_path: Path) -> None:
    runs_root = world.settings.data_dir / "runs"
    runs_root.mkdir(parents=True)
    target = tmp_path / "scratch-target"
    target.mkdir()
    symlink = runs_root / "some-run"
    _make_symlink(target, symlink)

    skip = await _service(world)._apply_scratch(symlink)

    assert skip is not None
    assert skip.reason == "symlink"
    assert target.is_dir()


async def test_apply_scratch_unsafe_path_is_skipped(world: CleanupWorld, tmp_path: Path) -> None:
    skip = await _service(world)._apply_scratch(tmp_path / "outside")

    assert skip is not None
    assert skip.reason == "unsafe path"


async def test_apply_scratch_missing_run_is_skipped(world: CleanupWorld) -> None:
    path = world.settings.data_dir / "runs" / "missing-run"
    path.mkdir(parents=True)

    skip = await _service(world)._apply_scratch(path)

    assert skip is not None
    assert skip.reason == "run missing"


async def test_apply_counts_scratch_skips(
    world: CleanupWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = _service(world)
    path = world.settings.data_dir / "runs" / "some-run"
    plan = CleanupPlan(worktrees=[], scratch_dirs=[path], skipped=[])

    async def fake_apply_scratch(value: Path) -> CleanupSkip:
        return CleanupSkip(path=value, reason="boom")

    monkeypatch.setattr(service, "_apply_scratch", fake_apply_scratch)

    result = await service.apply(plan)

    assert result.worktrees_removed == 0
    assert result.scratch_dirs_removed == 0
    assert [skip.reason for skip in result.skips] == ["boom"]


# ----------------------------------------------------------------- background --


def _service_for_background(
    sessions: async_sessionmaker[AsyncSession], settings: Settings
) -> CleanupService:
    return CleanupService(
        sessions=sessions,
        git=GitService(settings),
        settings=settings,
        now=lambda: NOW,
    )


async def test_run_forever_runs_one_round_and_handles_an_exception(
    database: Database,
    git_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _service_for_background(database.sessions, git_settings)
    plans = 0

    async def fake_plan(*, older_than_days: int) -> CleanupPlan:
        nonlocal plans
        plans += 1
        if plans == 1:
            raise RuntimeError("boom")
        return CleanupPlan(worktrees=[], scratch_dirs=[], skipped=[])

    async def fake_apply(plan: CleanupPlan) -> CleanupResult:
        return CleanupResult(worktrees_removed=0, scratch_dirs_removed=0, skips=[])

    sleeps: list[float] = []

    async def fake_sleep(value: float) -> None:
        sleeps.append(value)
        if len(sleeps) == 2:
            raise asyncio.CancelledError

    monkeypatch.setattr(service, "plan", fake_plan)
    monkeypatch.setattr(service, "apply", fake_apply)
    monkeypatch.setattr(service, "_sleep", fake_sleep)

    with pytest.raises(asyncio.CancelledError):
        await service.run_forever()

    assert plans == 2
    assert sleeps == [86400.0, 86400.0]


async def test_run_forever_propagates_cancellation_from_plan(
    database: Database,
    git_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _service_for_background(database.sessions, git_settings)

    async def cancelled_plan(*, older_than_days: int) -> CleanupPlan:
        raise asyncio.CancelledError

    monkeypatch.setattr(service, "plan", cancelled_plan)

    with pytest.raises(asyncio.CancelledError):
        await service.run_forever()


async def test_lifespan_starts_cleanup_when_enabled(
    database: Database,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = InMemorySecretStore()
    monkeypatch.setattr(app_module, "get_secret_store", lambda: store)
    settings = Settings(
        data_dir=tmp_path / "gaia-data",
        db_url=database.url,
        pr_sync_enabled=False,
        cleanup_enabled=True,
        cleanup_interval_s=60,
    )
    app = create_app(settings)

    async with app.router.lifespan_context(app):
        tasks = list(app.state.services.background_tasks)
        names = [task.get_name() for task in tasks]
        assert "cleanup" in names
        cleanup_task = next(task for task in tasks if task.get_name() == "cleanup")
        assert not cleanup_task.done()

    assert cleanup_task.cancelled()
