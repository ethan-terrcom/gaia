"""The real :class:`~gaia.orchestrator.deps.Committer` (doc 12b-1)."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from gaia.db.engine import session_scope
from gaia.db.models import Repository, Run, Worktree
from gaia.db.repo import RunRepo, ScopedRepo
from gaia.orchestrator.impl import worktree_location
from gaia.services.git import GitAuthor, GitError, GitService, WorktreeLocation

#: The first line of a Gaia commit message is capped at this many characters.
_MESSAGE_CAP = 200


@dataclass(frozen=True)
class _CommitContext:
    """The rows one commit needs, loaded in one short session."""

    run: Run
    repository: Repository
    worktree: Worktree


class WorktreeCommitter:
    """Commit an accepted step on the run's worktree branch."""

    def __init__(
        self,
        *,
        sessions: async_sessionmaker[AsyncSession],
        git: GitService,
    ) -> None:
        self.sessions = sessions
        self.git = git

    async def commit_step(self, run_id: str, step_index: int, title: str) -> str:
        """Commit the step and return the commit SHA.

        Idempotent for the resumed-node case: if the commit already happened,
        git reports "nothing to commit" and the current SHA is returned.
        """
        del step_index  # the message carries the title; the index is not needed
        context = await self._load_context(run_id)
        location = await self._location_for(context.repository, context.worktree)
        self.git.verify_location(location)
        message = f"[gaia] {title}".splitlines()[0][:_MESSAGE_CAP]
        try:
            return await self.git.commit(
                location,
                message,
                author=GitAuthor(name="Gaia", email="gaia@localhost"),
            )
        except GitError as exc:
            output = f"{exc.result.stdout}\n{exc.result.stderr}"
            if "nothing to commit" in output:
                return await self.git.current_sha(location)
            raise

    async def _load_context(self, run_id: str) -> _CommitContext:
        """Load the run, repository and worktree rows in one short session."""
        async with session_scope(self.sessions) as session:
            run = await RunRepo(session).get_by_id(run_id)
            if run is None:
                raise ValueError(f"no run {run_id!r}")
            workspace_id = run.workspace_id
            repositories = ScopedRepo(session, Repository, workspace_id)
            repository = await repositories.get(run.repo_id)
            if repository is None:
                raise ValueError(f"no repository {run.repo_id!r} in workspace {workspace_id!r}")
            worktree = None
            if run.worktree_id is not None:
                worktrees = ScopedRepo(session, Worktree, workspace_id)
                worktree = await worktrees.get(run.worktree_id)
            if worktree is None:
                raise ValueError(f"run {run_id!r} has no worktree")
            if worktree.repo_id != run.repo_id:
                raise ValueError(
                    f"run {run_id!r} worktree belongs to repository {worktree.repo_id!r}"
                )
            return _CommitContext(run=run, repository=repository, worktree=worktree)

    async def _location_for(self, repository: Repository, worktree: Worktree) -> WorktreeLocation:
        """Build the pinned location without holding a database session."""
        repo_path = await self.git.validate_repo(repository.local_path)
        return await worktree_location(git=self.git, repo_path=repo_path, worktree=worktree)
