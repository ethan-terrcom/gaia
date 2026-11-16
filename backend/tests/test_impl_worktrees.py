"""Tests for ``WorktreeSetup`` and the F7 worktree-revival fix (doc 12b-1)."""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from gaia.config.settings import Settings
from gaia.db.engine import session_scope
from gaia.db.models import Repository, Run, Ticket, Workspace, Worktree
from gaia.db.repo import ScopedRepo
from gaia.domain.enums import WorktreeStatus
from gaia.orchestrator.impl import check_runner, setup_runner, worktree_location
from gaia.orchestrator.impl.worktrees import SetupFailed, WorktreeSetup
from gaia.services.git import GitService
from gaia.services.worktrees import WorktreeAlreadyExists, WorktreeError, WorktreeService
from tests.db_builders import make_workspace
from tests.git_repo import commit_file, git, on_disk, resolved
from tests.tool_fakes import FakeCommandRunner, make_proc_result


def _ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


class _NoneScopedRepo:
    """A stand-in ScopedRepo whose ``get`` always returns ``None``."""

    async def get(self, row_id: str) -> None:
        del row_id
        return None


@dataclass(frozen=True)
class SetupWorld:
    """A workspace/repository/ticket/run plus the :class:`WorktreeSetup` under test."""

    workspace: Workspace
    repository: Repository
    ticket: Ticket
    run: Run
    setup: WorktreeSetup
    runner: FakeCommandRunner
    settings: Settings
    git: GitService


@pytest.fixture
async def setup_world(
    database: Any,
    git_repo: Path,
    git_settings: Settings,
    git_service: GitService,
) -> SetupWorld:
    """Build a real repository-backed world and a WorktreeSetup with a fake runner."""
    async with session_scope(database.sessions) as session:
        workspace = await make_workspace(session)
        repositories = ScopedRepo(session, Repository, workspace.id)
        repository = await repositories.create(
            name="fixture",
            local_path=str(git_repo),
            setup_command=["echo", "setup"],
        )
        tickets = ScopedRepo(session, Ticket, workspace.id)
        ticket = await tickets.create(repo_id=repository.id, title="Ship it")
        runs = ScopedRepo(session, Run, workspace.id)
        run = await runs.create(
            ticket_id=ticket.id,
            repo_id=repository.id,
            graph_thread_id="thread-setup",
            created_by=None,
        )

    runner = FakeCommandRunner()
    setup = WorktreeSetup(
        settings=git_settings,
        sessions=database.sessions,
        git=git_service,
        commands=runner,
    )
    return SetupWorld(
        workspace=workspace,
        repository=repository,
        ticket=ticket,
        run=run,
        setup=setup,
        runner=runner,
        settings=git_settings,
        git=git_service,
    )


async def _run(session: Any, workspace_id: str, run_id: str) -> Run:
    run = await ScopedRepo(session, Run, workspace_id).get(run_id)
    assert run is not None
    return run


async def test_prepare_creates_worktree_runs_setup_and_writes_marker(
    setup_world: SetupWorld,
) -> None:
    world = setup_world

    worktree_id = await world.setup.prepare(world.run.id)

    assert worktree_id
    async with session_scope(world.setup.sessions) as session:
        worktree = await ScopedRepo(session, Worktree, world.workspace.id).get(worktree_id)
        assert worktree is not None
        assert worktree.status == WorktreeStatus.IN_USE.value
        expected_path = resolved(world.settings.worktrees_root / world.repository.id / worktree_id)
        assert resolved(Path(worktree.path)) == expected_path

    marker = world.settings.data_dir / "runs" / world.run.id / "setup.ok"
    assert marker.is_file()

    assert len(world.runner.calls) == 1
    argv, cwd, timeout_s, task_dir = world.runner.calls[0]
    assert argv == ("echo", "setup")
    assert cwd == expected_path
    assert timeout_s == world.settings.setup_timeout_s
    assert task_dir.is_dir()


async def test_prepare_is_idempotent_and_skips_setup(setup_world: SetupWorld) -> None:
    world = setup_world
    worktree_id = await world.setup.prepare(world.run.id)

    # The node records the worktree id after prepare returns; simulate that.
    async with session_scope(world.setup.sessions) as session:
        await ScopedRepo(session, Run, world.workspace.id).update(
            world.run.id, worktree_id=worktree_id
        )

    again = await world.setup.prepare(world.run.id)

    assert again == worktree_id
    assert len(world.runner.calls) == 1


async def test_setup_failure_raises_and_leaves_no_marker(setup_world: SetupWorld) -> None:
    world = setup_world
    world.runner._results = [
        make_proc_result(
            ("echo", "setup"),
            cwd=Path("/tmp"),
            returncode=1,
            stdout="boom\n",
            stderr="secret-token\n",
        )
    ]

    with pytest.raises(SetupFailed) as raised:
        await world.setup.prepare(world.run.id)

    assert raised.value.returncode == 1
    assert "boom" in raised.value.tail
    assert "secret-token" in raised.value.tail  # SetupFailed tail is redacted, not stripped
    assert "exited with status 1" in str(raised.value)
    assert "boom" in str(raised.value)  # the output is in the message, so events and logs show why
    marker = world.settings.data_dir / "runs" / world.run.id / "setup.ok"
    assert not marker.exists()


async def test_create_revives_a_failed_row_and_clears_an_empty_directory(
    setup_world: SetupWorld,
) -> None:
    """F7: a failed row no longer wedges the branch, and an empty dir is ours to clear."""
    world = setup_world
    async with session_scope(world.setup.sessions) as session:
        service = WorktreeService(
            git=world.git,
            session=session,
            workspace_id=world.workspace.id,
        )
        first = await service.create(world.repository, world.ticket)
        path = Path(first.path)
        await ScopedRepo(session, Worktree, world.workspace.id).update(
            first.id, status=WorktreeStatus.FAILED.value
        )
        # Simulate a crashed create: the row is failed, git's metadata is gone,
        # and an empty directory is left at the worktree path.
        shutil.rmtree(path)
        repo_path = await world.git.validate_repo(world.repository.local_path)
        await world.git.worktree_prune(repo_path)
        _ensure_dir(path)

    # Re-create through the same service; _row_for must revive the failed row
    # and create must clear the empty directory before ``git worktree add``.
    async with session_scope(world.setup.sessions) as session:
        service = WorktreeService(
            git=world.git,
            session=session,
            workspace_id=world.workspace.id,
        )
        revived = await service.create(world.repository, world.ticket)

    assert revived.id == first.id
    assert revived.status == WorktreeStatus.READY.value
    assert on_disk(revived.path).is_dir
    assert on_disk(path).exists


async def test_create_refuses_a_non_empty_target_directory(setup_world: SetupWorld) -> None:
    """F7: Gaia never deletes files it cannot prove it made."""
    world = setup_world
    async with session_scope(world.setup.sessions) as session:
        service = WorktreeService(
            git=world.git,
            session=session,
            workspace_id=world.workspace.id,
        )
        first = await service.create(world.repository, world.ticket)
        path = Path(first.path)
        await ScopedRepo(session, Worktree, world.workspace.id).update(
            first.id, status=WorktreeStatus.FAILED.value
        )
        # Remove the directory behind Gaia's back, then plant a stranger file.
        shutil.rmtree(path)
        _ensure_dir(path)
        (path / "stranger.txt").write_text("not gaia's\n", encoding="utf-8")

    async with session_scope(world.setup.sessions) as session:
        service = WorktreeService(
            git=world.git,
            session=session,
            workspace_id=world.workspace.id,
        )
        with pytest.raises(WorktreeError, match="a previous attempt left files"):
            await service.create(world.repository, world.ticket)

    assert on_disk(path / "stranger.txt").is_file


async def test_reuse_after_worktree_already_exists_uses_the_live_row(
    setup_world: SetupWorld,
) -> None:
    """WorktreeAlreadyExists carries the row to reuse, never a stale branch error."""
    world = setup_world
    async with session_scope(world.setup.sessions) as session:
        service = WorktreeService(
            git=world.git,
            session=session,
            workspace_id=world.workspace.id,
        )
        first = await service.create(world.repository, world.ticket)

    async with session_scope(world.setup.sessions) as session:
        service = WorktreeService(
            git=world.git,
            session=session,
            workspace_id=world.workspace.id,
        )
        with pytest.raises(WorktreeAlreadyExists) as raised:
            await service.create(world.repository, world.ticket)
        assert raised.value.worktree.id == first.id
        assert raised.value.worktree.status == WorktreeStatus.READY.value


def test_setup_and_check_runner_factories_configure_domains(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path / "gaia-data")
    srt_path = tmp_path / "srt"

    setup = setup_runner(settings, srt_path)
    check = check_runner(settings, srt_path)

    assert setup.allowed_domains == settings.setup_allowed_domains
    assert check.allowed_domains == ()


async def test_worktree_location_derives_git_dir_for_old_rows(
    setup_world: SetupWorld,
) -> None:
    """Rows created before ``git_dir`` existed still get the F1 pin."""
    world = setup_world
    async with session_scope(world.setup.sessions) as session:
        service = WorktreeService(
            git=world.git,
            session=session,
            workspace_id=world.workspace.id,
        )
        worktree = await service.create(world.repository, world.ticket)
        await ScopedRepo(session, Worktree, world.workspace.id).update(worktree.id, git_dir=None)
        repo_path = await world.git.validate_repo(world.repository.local_path)
        location = await worktree_location(
            git=world.git,
            repo_path=repo_path,
            worktree=worktree,
        )
    assert location.git_dir == await world.git.worktree_admin_dir(
        await world.git.validate_repo(world.repository.local_path),
        resolved(Path(worktree.path)),
    )


async def test_prepare_unknown_run_raises(setup_world: SetupWorld) -> None:
    with pytest.raises(ValueError, match="no run"):
        await setup_world.setup.prepare("missing-run")


async def test_prepare_malformed_setup_command_raises(setup_world: SetupWorld) -> None:
    world = setup_world
    async with session_scope(world.setup.sessions) as session:
        await ScopedRepo(session, Repository, world.workspace.id).update(
            world.repository.id, setup_command=["echo", 1]
        )

    with pytest.raises(WorktreeError, match="must be a list of strings"):
        await world.setup.prepare(world.run.id)


async def test_mark_in_use_rejects_an_unknown_worktree(setup_world: SetupWorld) -> None:
    with pytest.raises(WorktreeError, match="no worktree"):
        await setup_world.setup._mark_in_use(setup_world.workspace.id, "missing-worktree")


async def test_mark_in_use_rejects_a_failed_worktree(setup_world: SetupWorld) -> None:
    world = setup_world
    async with session_scope(world.setup.sessions) as session:
        worktree = await ScopedRepo(session, Worktree, world.workspace.id).create(
            id="failed-wt",
            repo_id=world.repository.id,
            path=str(world.settings.worktrees_root / world.repository.id / "failed-wt"),
            branch="gaia/failed",
            status=WorktreeStatus.FAILED.value,
        )

    with pytest.raises(WorktreeError, match="cannot mark worktree"):
        await world.setup._mark_in_use(world.workspace.id, worktree.id)


async def test_load_context_rejects_worktree_from_another_repo(
    setup_world: SetupWorld,
) -> None:
    world = setup_world
    async with session_scope(world.setup.sessions) as session:
        repositories = ScopedRepo(session, Repository, world.workspace.id)
        repo_two = await repositories.create(
            name="repo-two",
            local_path=world.repository.local_path,
        )
        tickets = ScopedRepo(session, Ticket, world.workspace.id)
        ticket_two = await tickets.create(repo_id=repo_two.id, title="Ticket two")
        service = WorktreeService(
            git=world.git,
            session=session,
            workspace_id=world.workspace.id,
        )
        worktree_one = await service.create(world.repository, world.ticket)
        runs = ScopedRepo(session, Run, world.workspace.id)
        run_two = await runs.create(
            ticket_id=ticket_two.id,
            repo_id=repo_two.id,
            worktree_id=worktree_one.id,
            graph_thread_id="thread-mismatch",
            created_by=None,
        )

    with pytest.raises(WorktreeError, match="points at worktree"):
        await world.setup.prepare(run_two.id)


async def test_reuse_or_create_reraises_a_non_live_worktree(
    setup_world: SetupWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = setup_world
    context = cast(
        Any,
        SimpleNamespace(
            run=SimpleNamespace(workspace_id=world.workspace.id),
            repository=world.repository,
            ticket=world.ticket,
            worktree=None,
        ),
    )

    class _ExplodingWorktreeService:
        def __init__(self, **kwargs: Any) -> None:
            del kwargs

        async def create(self, repo: Repository, ticket: Ticket) -> Worktree:
            del repo, ticket
            raise WorktreeAlreadyExists(
                cast(
                    Any,
                    SimpleNamespace(
                        id="wt-failed",
                        status=WorktreeStatus.FAILED.value,
                        branch="gaia/failed",
                        path="/tmp/failed",
                    ),
                )
            )

    monkeypatch.setattr(
        "gaia.orchestrator.impl.worktrees.WorktreeService",
        _ExplodingWorktreeService,
    )
    with pytest.raises(WorktreeAlreadyExists):
        await world.setup._reuse_or_create(context)


async def test_reuse_or_create_returns_the_live_row_of_an_earlier_run(
    setup_world: SetupWorld,
) -> None:
    """A second run on the same ticket meets its branch's live worktree.

    Regression: the handler used the row the failed ``create`` had loaded, which
    the session's rollback had expired, so the new run died with
    ``DetachedInstanceError`` before setup ran. This uses the real service and
    database, not a stand-in, because the stand-ins are what hid it.
    """
    world = setup_world
    async with session_scope(world.setup.sessions) as session:
        service = WorktreeService(git=world.git, session=session, workspace_id=world.workspace.id)
        first = await service.create(world.repository, world.ticket)
    context = cast(
        Any,
        SimpleNamespace(
            run=SimpleNamespace(workspace_id=world.workspace.id),
            repository=world.repository,
            ticket=world.ticket,
            worktree=None,
        ),
    )

    returned = await world.setup._reuse_or_create(context)

    assert returned.id == first.id
    assert returned.status == WorktreeStatus.READY.value
    assert returned.path == first.path  # readable: the row is not expired or detached


async def test_reuse_or_create_reraises_when_the_live_row_has_vanished(
    setup_world: SetupWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = setup_world
    context = cast(
        Any,
        SimpleNamespace(
            run=SimpleNamespace(workspace_id=world.workspace.id),
            repository=world.repository,
            ticket=world.ticket,
            worktree=None,
        ),
    )
    ghost = cast(
        Any,
        SimpleNamespace(
            id="wt-ghost",
            status=WorktreeStatus.READY.value,
            branch="gaia/ghost",
            path="/tmp/ghost",
        ),
    )

    class _GhostWorktreeService:
        def __init__(self, **kwargs: Any) -> None:
            del kwargs

        async def create(self, repo: Repository, ticket: Ticket) -> Worktree:
            del repo, ticket
            raise WorktreeAlreadyExists(ghost)

    monkeypatch.setattr("gaia.orchestrator.impl.worktrees.WorktreeService", _GhostWorktreeService)

    with pytest.raises(WorktreeAlreadyExists):
        await world.setup._reuse_or_create(context)


async def test_load_context_rejects_missing_ticket(
    setup_world: SetupWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = setup_world
    real_scoped_repo = ScopedRepo

    def fake_scoped_repo(session: Any, model: Any, workspace_id: str) -> Any:
        if model is Ticket:
            return _NoneScopedRepo()
        return real_scoped_repo(session, model, workspace_id)

    monkeypatch.setattr("gaia.orchestrator.impl.worktrees.ScopedRepo", fake_scoped_repo)

    with pytest.raises(ValueError, match="no ticket"):
        await world.setup.prepare(world.run.id)


async def test_load_context_rejects_missing_repository(
    setup_world: SetupWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = setup_world
    real_scoped_repo = ScopedRepo

    def fake_scoped_repo(session: Any, model: Any, workspace_id: str) -> Any:
        if model is Repository:
            return _NoneScopedRepo()
        return real_scoped_repo(session, model, workspace_id)

    monkeypatch.setattr("gaia.orchestrator.impl.worktrees.ScopedRepo", fake_scoped_repo)

    with pytest.raises(ValueError, match="no repository"):
        await world.setup.prepare(world.run.id)


async def test_mark_in_use_leaves_an_in_use_worktree_alone(
    setup_world: SetupWorld,
) -> None:
    world = setup_world
    async with session_scope(world.setup.sessions) as session:
        worktree = await ScopedRepo(session, Worktree, world.workspace.id).create(
            id="in-use-wt",
            repo_id=world.repository.id,
            path=str(world.settings.worktrees_root / world.repository.id / "in-use-wt"),
            branch="gaia/in-use",
            status=WorktreeStatus.IN_USE.value,
        )

    await world.setup._mark_in_use(world.workspace.id, worktree.id)


def test_setup_failed_message_has_no_detail_when_there_was_no_output() -> None:
    assert str(SetupFailed(2, "  \n")) == "setup command exited with status 2"


def test_setup_failed_message_keeps_only_the_end_of_long_output() -> None:
    message = str(SetupFailed(2, "x" * 5000 + "THE-END"))
    assert message.endswith("THE-END")
    assert len(message) < 1000


# ----------------------------------------------------- re-run on a stale worktree --
# A run that fails before it records its worktree (in setup, say) leaves the
# ticket's worktree live. The next run reuses it; these pin down when that reuse
# moves the worktree up to today's base and when it must leave it alone.


def _rerun_context(world: SetupWorld) -> Any:
    """The context of a new run on the same ticket: no worktree recorded yet."""
    return cast(
        Any,
        SimpleNamespace(
            run=SimpleNamespace(workspace_id=world.workspace.id),
            repository=world.repository,
            ticket=world.ticket,
            worktree=None,
        ),
    )


async def _first_worktree(world: SetupWorld) -> Worktree:
    async with session_scope(world.setup.sessions) as session:
        service = WorktreeService(git=world.git, session=session, workspace_id=world.workspace.id)
        return await service.create(world.repository, world.ticket)


def _head(path: str) -> str:
    return git(Path(path), "rev-parse", "HEAD").strip()


async def test_rerun_fast_forwards_an_untouched_worktree_to_the_new_tip(
    setup_world: SetupWorld,
) -> None:
    """The owner's case: main moved on after the failed run, and the re-run must see it."""
    world = setup_world
    first = await _first_worktree(world)
    old_base = first.base_sha
    new_tip = commit_file(
        Path(world.repository.local_path), "Makefile", "setup-worktree:\n", "add target"
    )

    returned = await world.setup._reuse_or_create(_rerun_context(world))

    assert returned.id == first.id
    assert returned.base_sha == new_tip != old_base
    assert _head(returned.path) == new_tip
    assert on_disk(Path(returned.path) / "Makefile").is_file
    async with session_scope(world.setup.sessions) as session:
        row = await ScopedRepo(session, Worktree, world.workspace.id).get(first.id)
        assert row is not None
        assert row.base_sha == new_tip


async def test_rerun_keeps_a_worktree_with_commits_of_its_own(setup_world: SetupWorld) -> None:
    world = setup_world
    first = await _first_worktree(world)
    own = commit_file(Path(first.path), "work.txt", "work\n", "the last run's work")
    commit_file(Path(world.repository.local_path), "other.txt", "x\n", "main moves on")

    returned = await world.setup._reuse_or_create(_rerun_context(world))

    assert returned.base_sha == first.base_sha
    assert _head(returned.path) == own


async def test_rerun_keeps_a_dirty_worktree(setup_world: SetupWorld) -> None:
    world = setup_world
    first = await _first_worktree(world)
    (Path(first.path) / "half-done.txt").write_text("wip\n", encoding="utf-8")
    commit_file(Path(world.repository.local_path), "other.txt", "x\n", "main moves on")

    returned = await world.setup._reuse_or_create(_rerun_context(world))

    assert returned.base_sha == first.base_sha
    assert _head(returned.path) == first.base_sha
    assert on_disk(Path(first.path) / "half-done.txt").is_file


async def test_rerun_on_an_up_to_date_worktree_changes_nothing(setup_world: SetupWorld) -> None:
    world = setup_world
    first = await _first_worktree(world)

    returned = await world.setup._reuse_or_create(_rerun_context(world))

    assert returned.base_sha == first.base_sha
    assert returned.updated_at == first.updated_at


async def test_refresh_records_the_tip_when_head_already_matches_it(
    setup_world: SetupWorld,
) -> None:
    """A row whose base drifted from a head that is already at the tip is corrected."""
    world = setup_world
    first = await _first_worktree(world)
    async with session_scope(world.setup.sessions) as session:
        await ScopedRepo(session, Worktree, world.workspace.id).update(first.id, base_sha="0" * 40)

    returned = await world.setup._reuse_or_create(_rerun_context(world))

    assert returned.base_sha == first.base_sha


async def _remove(world: SetupWorld, worktree: Worktree) -> None:
    async with session_scope(world.setup.sessions) as session:
        service = WorktreeService(git=world.git, session=session, workspace_id=world.workspace.id)
        await service.remove(worktree, force=True)


async def test_revived_branch_with_no_work_is_moved_to_the_new_tip(
    setup_world: SetupWorld,
) -> None:
    """Reviving a removed row checks out the old branch; it must not keep the old code."""
    world = setup_world
    first = await _first_worktree(world)
    await _remove(world, first)
    new_tip = commit_file(Path(world.repository.local_path), "new.txt", "x\n", "main moves on")

    revived = await _first_worktree(world)

    assert revived.id == first.id
    assert revived.base_sha == new_tip
    assert _head(revived.path) == new_tip


async def test_revived_branch_with_work_keeps_it_and_records_the_merge_base(
    setup_world: SetupWorld,
) -> None:
    """Regression: the revived row used to claim today's tip as its base, so its diff was wrong."""
    world = setup_world
    first = await _first_worktree(world)
    own = commit_file(Path(first.path), "work.txt", "work\n", "the last run's work")
    await _remove(world, first)
    commit_file(Path(world.repository.local_path), "new.txt", "x\n", "main moves on")

    revived = await _first_worktree(world)

    assert revived.base_sha == first.base_sha
    assert _head(revived.path) == own
