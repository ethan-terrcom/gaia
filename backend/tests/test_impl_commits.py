"""Tests for ``WorktreeCommitter`` (doc 12b-1)."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from gaia.config.settings import Settings
from gaia.db.engine import session_scope
from gaia.db.models import Repository, Run, Ticket, Workspace, Worktree
from gaia.db.repo import ScopedRepo
from gaia.orchestrator.impl.commits import WorktreeCommitter
from gaia.services.git import GitError, GitService
from gaia.services.proc import ProcResult
from gaia.services.worktrees import WorktreeService
from tests.db_builders import make_workspace


class _NoneScopedRepo:
    """A stand-in ScopedRepo whose ``get`` always returns ``None``."""

    async def get(self, row_id: str) -> None:
        del row_id
        return None


@dataclass(frozen=True)
class CommitWorld:
    """A run with a real worktree, ready for the committer."""

    workspace: Workspace
    repository: Repository
    run: Run
    worktree: Worktree
    settings: Settings
    git: GitService
    sessions: Any


@pytest.fixture
async def commit_world(
    database: Any,
    git_repo: Path,
    git_settings: Settings,
    git_service: GitService,
) -> CommitWorld:
    async with session_scope(database.sessions) as session:
        workspace = await make_workspace(session)
        repositories = ScopedRepo(session, Repository, workspace.id)
        repository = await repositories.create(
            name="fixture",
            local_path=str(git_repo),
        )
        tickets = ScopedRepo(session, Ticket, workspace.id)
        ticket = await tickets.create(repo_id=repository.id, title="Ship it")
        runs = ScopedRepo(session, Run, workspace.id)
        run = await runs.create(
            ticket_id=ticket.id,
            repo_id=repository.id,
            graph_thread_id="thread-commit",
            created_by=None,
        )
        service = WorktreeService(
            git=git_service,
            session=session,
            workspace_id=workspace.id,
        )
        worktree = await service.create(repository, ticket)
        await ScopedRepo(session, Run, workspace.id).update(run.id, worktree_id=worktree.id)
    return CommitWorld(
        workspace=workspace,
        repository=repository,
        run=run,
        worktree=worktree,
        settings=git_settings,
        git=git_service,
        sessions=database.sessions,
    )


async def test_commit_step_commits_everything_and_returns_the_sha(
    commit_world: CommitWorld,
) -> None:
    path = Path(commit_world.worktree.path)
    (path / "new-file.txt").write_text("hello\n", encoding="utf-8")

    committer = WorktreeCommitter(
        sessions=commit_world.sessions,
        git=commit_world.git,
    )
    sha = await committer.commit_step(commit_world.run.id, 0, "Add a file")

    location = await _location(commit_world)
    assert await commit_world.git.current_sha(location) == sha
    commits = await commit_world.git.log(location, limit=5)
    assert commits[0].summary == "[gaia] Add a file"
    assert commits[0].sha == sha


async def test_commit_step_is_idempotent_when_there_is_nothing_to_commit(
    commit_world: CommitWorld,
) -> None:
    path = Path(commit_world.worktree.path)
    (path / "new-file.txt").write_text("hello\n", encoding="utf-8")

    committer = WorktreeCommitter(
        sessions=commit_world.sessions,
        git=commit_world.git,
    )
    first = await committer.commit_step(commit_world.run.id, 0, "Add a file")
    second = await committer.commit_step(commit_world.run.id, 0, "Add a file")

    assert first == second


async def test_commit_message_first_line_is_capped(commit_world: CommitWorld) -> None:
    path = Path(commit_world.worktree.path)
    (path / "new-file.txt").write_text("hello\n", encoding="utf-8")
    title = "x" * 250

    committer = WorktreeCommitter(
        sessions=commit_world.sessions,
        git=commit_world.git,
    )
    await committer.commit_step(commit_world.run.id, 0, title)

    location = await _location(commit_world)
    commits = await commit_world.git.log(location, limit=1)
    assert commits[0].summary == f"[gaia] {title}"[:200]


async def _location(commit_world: CommitWorld) -> Any:
    from gaia.orchestrator.impl import worktree_location

    repo_path = await commit_world.git.validate_repo(commit_world.repository.local_path)
    return await worktree_location(
        git=commit_world.git,
        repo_path=repo_path,
        worktree=commit_world.worktree,
    )


async def test_commit_step_reraises_an_unrelated_git_error(
    commit_world: CommitWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    committer = WorktreeCommitter(
        sessions=commit_world.sessions,
        git=commit_world.git,
    )
    result = ProcResult(
        argv=("git", "commit"),
        cwd=Path("/tmp"),
        pid=1,
        returncode=128,
        stdout="",
        stderr="fatal: something else failed\n",
        stdout_truncated=False,
        stderr_truncated=False,
        duration_s=0.1,
        timed_out=False,
    )

    async def fake_commit(location: Any, message: str, *, author: Any = None) -> str:
        del location, message, author
        raise GitError(("commit",), result)

    monkeypatch.setattr(committer.git, "commit", fake_commit)

    with pytest.raises(GitError, match="something else failed"):
        await committer.commit_step(commit_world.run.id, 0, "Boom")


async def test_commit_step_unknown_run_raises(commit_world: CommitWorld) -> None:
    committer = WorktreeCommitter(
        sessions=commit_world.sessions,
        git=commit_world.git,
    )

    with pytest.raises(ValueError, match="no run"):
        await committer.commit_step("missing-run", 0, "Boom")


async def test_commit_step_requires_a_worktree(commit_world: CommitWorld) -> None:
    async with session_scope(commit_world.sessions) as session:
        tickets = ScopedRepo(session, Ticket, commit_world.workspace.id)
        ticket = await tickets.create(repo_id=commit_world.repository.id, title="Another ticket")
        run = await ScopedRepo(session, Run, commit_world.workspace.id).create(
            ticket_id=ticket.id,
            repo_id=commit_world.repository.id,
            graph_thread_id="thread-no-worktree",
            created_by=None,
        )

    committer = WorktreeCommitter(
        sessions=commit_world.sessions,
        git=commit_world.git,
    )

    with pytest.raises(ValueError, match="has no worktree"):
        await committer.commit_step(run.id, 0, "Boom")


async def test_commit_step_rejects_a_worktree_from_another_repo(
    commit_world: CommitWorld,
) -> None:
    async with session_scope(commit_world.sessions) as session:
        repositories = ScopedRepo(session, Repository, commit_world.workspace.id)
        repo_two = await repositories.create(
            name="repo-two",
            local_path=commit_world.repository.local_path,
        )
        tickets = ScopedRepo(session, Ticket, commit_world.workspace.id)
        ticket_two = await tickets.create(repo_id=repo_two.id, title="Ticket two")
        run = await ScopedRepo(session, Run, commit_world.workspace.id).create(
            ticket_id=ticket_two.id,
            repo_id=repo_two.id,
            worktree_id=commit_world.worktree.id,
            graph_thread_id="thread-mismatch",
            created_by=None,
        )

    committer = WorktreeCommitter(
        sessions=commit_world.sessions,
        git=commit_world.git,
    )

    with pytest.raises(ValueError, match="belongs to repository"):
        await committer.commit_step(run.id, 0, "Boom")


async def test_commit_step_rejects_missing_repository(
    commit_world: CommitWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_scoped_repo = ScopedRepo

    def fake_scoped_repo(session: Any, model: Any, workspace_id: str) -> Any:
        if model is Repository:
            return _NoneScopedRepo()
        return real_scoped_repo(session, model, workspace_id)

    monkeypatch.setattr("gaia.orchestrator.impl.commits.ScopedRepo", fake_scoped_repo)
    committer = WorktreeCommitter(
        sessions=commit_world.sessions,
        git=commit_world.git,
    )

    with pytest.raises(ValueError, match="no repository"):
        await committer.commit_step(commit_world.run.id, 0, "Boom")
