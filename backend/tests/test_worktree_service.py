"""Doc 05 task 3 and 4: worktree lifecycle and path confinement.

Every test here runs against a real repository, a real SQLite database and the
real :class:`~gaia.services.git.GitService`, because the bugs worth catching are
the ones where the row and the directory disagree.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from gaia.config.settings import Settings
from gaia.db.models import Repository, Ticket, Workspace, Worktree
from gaia.db.repo import ScopedRepo, WorkspaceMismatch
from gaia.domain.enums import WorktreeStatus
from gaia.services.git import GitError, GitService, UnsafePath, resolve_within
from gaia.services.proc import ProcResult
from gaia.services.worktrees import (
    WorktreeAlreadyExists,
    WorktreeDirty,
    WorktreeError,
    WorktreeReport,
    WorktreeService,
)
from tests.db_builders import make_workspace
from tests.git_repo import git, on_disk, resolved


@dataclass(frozen=True)
class World:
    """One workspace, one repository row pointing at a real git repo, one ticket."""

    workspace: Workspace
    repository: Repository
    ticket: Ticket
    service: WorktreeService
    git: GitService
    settings: Settings


@pytest.fixture
async def world(
    session: AsyncSession,
    git_repo: Path,
    git_settings: Settings,
    git_service: GitService,
) -> World:
    """A workspace whose single repository is the ``git_repo`` fixture."""
    workspace = await make_workspace(session)
    repository = await ScopedRepo(session, Repository, workspace.id).create(
        name="fixture", local_path=str(git_repo)
    )
    ticket = await ScopedRepo(session, Ticket, workspace.id).create(
        repo_id=repository.id, title="Add API skeleton & auth!"
    )
    return World(
        workspace=workspace,
        repository=repository,
        ticket=ticket,
        service=WorktreeService(git=git_service, session=session, workspace_id=workspace.id),
        git=git_service,
        settings=git_settings,
    )


def failed_result(argv: tuple[str, ...] = ("worktree", "add")) -> ProcResult:
    """A :class:`ProcResult` that looks like a failed git command."""
    return ProcResult(
        argv=("git", *argv),
        cwd=Path("/tmp"),
        pid=1,
        returncode=128,
        stdout="",
        stderr="fatal: something went wrong\n",
        stdout_truncated=False,
        stderr_truncated=False,
        duration_s=0.01,
        timed_out=False,
    )


# ---------------------------------------------------------------- create --
async def test_create_makes_a_worktree_on_a_gaia_branch_under_the_root(
    world: World, git_repo: Path
) -> None:
    worktree = await world.service.create(world.repository, world.ticket)

    assert worktree.status == WorktreeStatus.READY.value
    assert worktree.branch.startswith("gaia/")
    assert worktree.branch.endswith("-add-api-skeleton-auth")
    assert worktree.base_sha == await world.git.current_sha(git_repo)

    path = Path(worktree.path)
    assert on_disk(path).is_dir
    assert path == resolve_within(world.settings.worktrees_root, path)
    assert path.parent == (world.settings.worktrees_root / world.repository.id).resolve()
    assert await world.git.current_branch(path) == worktree.branch
    assert {entry.branch for entry in await world.git.worktree_list(git_repo)} == {
        "main",
        worktree.branch,
    }


async def test_create_uses_the_requested_base_ref(world: World, git_repo: Path) -> None:
    base_sha = await world.git.current_sha(git_repo)

    worktree = await world.service.create(world.repository, world.ticket, base_sha)

    assert worktree.base_sha == base_sha


async def test_create_reports_an_unknown_base_ref_without_leaving_a_row(world: World) -> None:
    with pytest.raises(GitError, match="rev-parse"):
        await world.service.create(world.repository, world.ticket, "no-such-ref")

    assert (await world.service.list(world.repository)).items == ()


async def test_create_refuses_a_worktree_that_is_still_live(world: World) -> None:
    first = await world.service.create(world.repository, world.ticket)

    with pytest.raises(WorktreeAlreadyExists) as caught:
        await world.service.create(world.repository, world.ticket)

    assert caught.value.worktree.id == first.id
    assert "already ready" in str(caught.value)


async def test_create_revives_a_removed_worktree(world: World) -> None:
    """A run resumed after cleanup reuses its row and its branch."""
    first = await world.service.create(world.repository, world.ticket)
    await world.service.remove(first)

    second = await world.service.create(world.repository, world.ticket)

    assert second.id == first.id
    assert second.branch == first.branch
    assert second.status == WorktreeStatus.READY.value
    assert second.path == first.path  # the same row, so the same directory
    assert on_disk(second.path).is_dir


async def test_create_marks_the_row_failed_when_git_fails(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A crash mid-creation is recorded: a row that said ``ready`` would lie."""

    async def explode(*args: object, **kwargs: object) -> None:
        raise GitError(("worktree", "add"), failed_result())

    monkeypatch.setattr(world.git, "worktree_add", explode)

    with pytest.raises(GitError):
        await world.service.create(world.repository, world.ticket)

    report = await world.service.list(world.repository)
    assert [item.worktree.status for item in report.items] == [WorktreeStatus.FAILED.value]


async def test_create_refuses_a_repository_from_another_workspace(
    world: World, session: AsyncSession
) -> None:
    other = await make_workspace(session, slug="other")
    foreign = await ScopedRepo(session, Repository, other.id).create(
        name="foreign", local_path=world.repository.local_path
    )

    with pytest.raises(WorkspaceMismatch, match="cannot write"):
        await world.service.create(foreign, world.ticket)


# ---------------------------------------------------------------- remove --
async def test_remove_deletes_the_worktree_and_keeps_the_row(world: World, git_repo: Path) -> None:
    worktree = await world.service.create(world.repository, world.ticket)

    removed = await world.service.remove(worktree)

    assert removed.status == WorktreeStatus.REMOVED.value
    assert not on_disk(worktree.path).exists
    assert {entry.branch for entry in await world.git.worktree_list(git_repo)} == {"main"}
    # The row survives - where a run worked is history - but a removed worktree is
    # not something the repository has, so the default list leaves it out and the
    # audit view shows it.
    assert (await world.service.list(world.repository)).items == ()
    report = await world.service.list(world.repository, include_removed=True)
    assert [item.worktree.id for item in report.items] == [worktree.id]
    assert report.items[0].orphan


async def test_remove_refuses_a_dirty_worktree(world: World) -> None:
    """The doc's test: uncommitted work is not discarded by accident."""
    worktree = await world.service.create(world.repository, world.ticket)
    (Path(worktree.path) / "uncommitted.txt").write_text("precious\n")

    with pytest.raises(WorktreeDirty, match="uncommitted changes"):
        await world.service.remove(worktree)

    assert on_disk(Path(worktree.path) / "uncommitted.txt").is_file


async def test_remove_with_force_discards_the_changes(world: World) -> None:
    worktree = await world.service.create(world.repository, world.ticket)
    (Path(worktree.path) / "uncommitted.txt").write_text("bye\n")

    removed = await world.service.remove(worktree, force=True)

    assert removed.status == WorktreeStatus.REMOVED.value
    assert not on_disk(worktree.path).exists


async def test_remove_of_a_directory_that_already_vanished_only_prunes(
    world: World, git_repo: Path
) -> None:
    worktree = await world.service.create(world.repository, world.ticket)
    shutil.rmtree(worktree.path)

    removed = await world.service.remove(worktree)

    assert removed.status == WorktreeStatus.REMOVED.value
    assert {entry.branch for entry in await world.git.worktree_list(git_repo)} == {"main"}


async def test_remove_is_idempotent(world: World) -> None:
    worktree = await world.service.create(world.repository, world.ticket)
    await world.service.remove(worktree)

    assert (await world.service.remove(worktree)).status == WorktreeStatus.REMOVED.value


async def test_remove_refuses_a_row_from_another_workspace(
    world: World, session: AsyncSession
) -> None:
    other = await make_workspace(session, slug="other")
    foreign = await ScopedRepo(session, Worktree, other.id).create(
        repo_id=world.repository.id,
        path=str(world.settings.worktrees_root / "elsewhere"),
        branch="gaia/elsewhere",
    )

    with pytest.raises(WorkspaceMismatch, match="cannot write"):
        await world.service.remove(foreign)


async def test_remove_reports_a_row_whose_repository_is_not_ours(
    world: World, session: AsyncSession
) -> None:
    """A worktree row can only point at a repo its own workspace can see."""
    other = await make_workspace(session, slug="other")
    foreign_repo = await ScopedRepo(session, Repository, other.id).create(
        name="foreign", local_path=world.repository.local_path
    )
    orphan = await ScopedRepo(session, Worktree, world.workspace.id).create(
        repo_id=foreign_repo.id,
        path=str(world.settings.worktrees_root / "orphaned"),
        branch="gaia/orphaned",
    )

    with pytest.raises(WorktreeError, match="not in this workspace"):
        await world.service.remove(orphan)


async def test_remove_refuses_a_row_whose_path_escaped_the_root(
    world: World, session: AsyncSession
) -> None:
    hostile = await ScopedRepo(session, Worktree, world.workspace.id).create(
        repo_id=world.repository.id, path="/etc", branch="gaia/hostile"
    )

    with pytest.raises(UnsafePath, match="outside"):
        await world.service.remove(hostile)


# ------------------------------------------------------------------ list --
async def test_list_reports_a_healthy_worktree(world: World) -> None:
    worktree = await world.service.create(world.repository, world.ticket)

    report = await world.service.list(world.repository)

    assert isinstance(report, WorktreeReport)
    assert [(item.worktree.id, item.in_git, item.exists, item.orphan) for item in report.items] == [
        (worktree.id, True, True, False)
    ]
    assert report.orphans == ()
    assert report.unknown_paths == ()


async def test_list_flags_a_row_git_does_not_know(world: World, git_repo: Path) -> None:
    """The doc's orphan: a worktree removed behind Gaia's back."""
    worktree = await world.service.create(world.repository, world.ticket)
    git(git_repo, "worktree", "remove", "--force", worktree.path)

    report = await world.service.list(world.repository)

    (item,) = report.items
    assert item.worktree.id == worktree.id
    assert not item.in_git
    assert not item.exists
    assert item.orphan
    assert report.orphans == (item,)


async def test_list_reports_worktrees_no_row_claims(world: World, git_repo: Path) -> None:
    stray = world.settings.worktrees_root.parent / "stray"
    git(git_repo, "worktree", "add", "-q", str(stray), "-b", "gaia/stray", "main")

    report = await world.service.list(world.repository)

    assert report.items == ()
    assert report.unknown_paths == (resolved(stray),)


async def test_list_refuses_a_repository_from_another_workspace(
    world: World, session: AsyncSession
) -> None:
    other = await make_workspace(session, slug="other")
    foreign = await ScopedRepo(session, Repository, other.id).create(
        name="foreign", local_path=world.repository.local_path
    )

    with pytest.raises(WorkspaceMismatch, match="cannot write"):
        await world.service.list(foreign)


# --------------------------------------------------------------- editors --
async def test_open_in_editor_returns_a_vscode_url_without_running_anything(
    world: World,
) -> None:
    worktree = await world.service.create(world.repository, world.ticket)

    url = world.service.open_in_editor(worktree)

    assert url == f"vscode://file{worktree.path}"
    assert url.startswith("vscode://file/")
    assert " " not in url


async def test_open_in_editor_refuses_a_row_from_another_workspace(
    world: World, session: AsyncSession
) -> None:
    other = await make_workspace(session, slug="other")
    foreign = await ScopedRepo(session, Worktree, other.id).create(
        repo_id=world.repository.id,
        path=str(world.settings.worktrees_root / "elsewhere"),
        branch="gaia/elsewhere",
    )

    with pytest.raises(WorkspaceMismatch, match="cannot write"):
        world.service.open_in_editor(foreign)


@pytest.mark.parametrize(
    "segment", ["../escape", "a/b", "", "x" * 65, "id with space", "trailing-newline\n"]
)
async def test_path_for_refuses_anything_that_is_not_one_safe_segment(
    world: World, segment: str
) -> None:
    with pytest.raises(WorktreeError, match="not usable as a directory name"):
        world.service.path_for(world.repository, segment)
