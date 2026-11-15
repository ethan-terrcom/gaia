"""Clean up finished runs' worktrees and scratch directories (doc 18c).

Gaia creates one git worktree per run and scratch directories under
``<data_dir>/runs/<run_id>/``. Left alone they fill the disk. This service plans
and applies their removal **only** for runs that reached a terminal status long
enough ago, and it refuses to touch anything a human might still care about:

- a worktree with uncommitted changes;
- a worktree whose ``.git`` pin has been tampered with;
- a worktree whose branch has commits that are not on its base commit, unless
  the branch's PR has already been merged;
- a symlink planted where a scratch directory should be.

``plan`` is read-only. ``apply`` removes exactly what ``plan`` returned, and
``WorktreeService.remove`` is always called with ``force=False``.
"""

from __future__ import annotations

import asyncio
import os
import shutil
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import cast

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from gaia.config.settings import Settings
from gaia.db.engine import session_scope
from gaia.db.models import AuditLog, Run, Worktree
from gaia.db.repo import AuditLogRepo, CleanupRepo, CleanupWorktreeCandidate, RunRepo
from gaia.db.types import utcnow
from gaia.domain.state import RUN_TRANSITIONS
from gaia.security.paths import UnsafePath
from gaia.services.git import (
    GitError,
    GitService,
    WorktreeLocation,
    WorktreeTampered,
    resolve_within,
)
from gaia.services.worktrees import WorktreeDirty, WorktreeService

logger = structlog.get_logger(__name__)

#: Run statuses with no outgoing transition in :mod:`gaia.domain.state`: the
#: terminal states. Derived rather than repeated, so the cleanup service cannot
#: drift from the state machine.
_TERMINAL_RUN_STATUSES = frozenset(
    status.value for status, moves in RUN_TRANSITIONS.items() if not moves
)


@dataclass(frozen=True)
class CleanupItem:
    """One worktree ``apply`` may remove."""

    worktree: Worktree
    path: Path
    last_run: Run


@dataclass(frozen=True)
class CleanupSkip:
    """Something ``plan`` or ``apply`` looked at and deliberately left alone."""

    path: Path
    reason: str
    worktree: Worktree | None = None
    last_run: Run | None = None


@dataclass(frozen=True)
class CleanupPlan:
    """The read-only result of :meth:`CleanupService.plan`."""

    worktrees: list[CleanupItem]
    scratch_dirs: list[Path]
    skipped: list[CleanupSkip]


@dataclass(frozen=True)
class CleanupResult:
    """The result of :meth:`CleanupService.apply`."""

    worktrees_removed: int
    scratch_dirs_removed: int
    skips: list[CleanupSkip]


class CleanupService:
    """Plan and apply cleanup of finished runs (doc 18c)."""

    def __init__(
        self,
        *,
        sessions: async_sessionmaker[AsyncSession],
        git: GitService,
        settings: Settings,
        now: Callable[[], datetime] = utcnow,
    ) -> None:
        self.sessions = sessions
        self.git = git
        self.settings = settings
        self._now = now
        self._sleep: Callable[[float], Awaitable[None]] = asyncio.sleep

    # ----------------------------------------------------------------- plan --
    async def plan(self, *, older_than_days: int) -> CleanupPlan:
        """Return what cleanup would remove, without touching disk.

        All database rows are loaded in one short session; every git command runs
        after that session has closed.
        """
        if older_than_days < 1:
            raise ValueError(f"older_than_days must be positive, not {older_than_days!r}")
        cutoff = self._now() - timedelta(days=older_than_days)

        async with session_scope(self.sessions) as session:
            repo = CleanupRepo(session)
            candidates = await repo.load_worktree_candidates()
            scratch_run_ids = await repo.list_old_terminal_run_ids(cutoff)

        worktrees: list[CleanupItem] = []
        skipped: list[CleanupSkip] = []
        for candidate in candidates:
            outcome = await self._plan_worktree(candidate, cutoff)
            if isinstance(outcome, CleanupItem):
                worktrees.append(outcome)
            else:
                skipped.append(outcome)

        scratch_dirs = _scratch_dirs(self.settings.data_dir / "runs", scratch_run_ids)
        return CleanupPlan(worktrees=worktrees, scratch_dirs=scratch_dirs, skipped=skipped)

    async def _plan_worktree(
        self, candidate: CleanupWorktreeCandidate, cutoff: datetime
    ) -> CleanupItem | CleanupSkip:
        """Decide one worktree's fate without changing the worktree or its row."""
        worktree = candidate.worktree
        runs = candidate.runs
        if not runs:
            return _skip(Path(worktree.path), "no runs", worktree, None)
        if any(run.status not in _TERMINAL_RUN_STATUSES for run in runs):
            return _skip(Path(worktree.path), "active run", worktree, _newest_run(runs))
        if any(run.finished_at is None for run in runs):
            return _skip(Path(worktree.path), "no finish time", worktree, _newest_run(runs))

        last_run = max(runs, key=lambda run: cast(datetime, run.finished_at))
        newest_finished_at = cast(datetime, last_run.finished_at)
        if newest_finished_at >= cutoff:
            return _skip(Path(worktree.path), "not old enough", worktree, last_run)

        row_path = Path(worktree.path)
        if _is_symlink(row_path):
            return _skip(row_path, "symlink worktree", worktree, last_run)
        try:
            path = resolve_within(self.git.worktrees_root, row_path)
        except UnsafePath:
            return _skip(row_path, "unsafe path", worktree, last_run)
        if not path.exists():
            return _skip(path, "directory missing", worktree, last_run)

        if worktree.git_dir is None:
            return _skip(path, "missing git dir", worktree, last_run)
        location = WorktreeLocation(work_tree=path, git_dir=Path(worktree.git_dir))
        try:
            self.git.verify_location(location)
        except WorktreeTampered:
            return _skip(path, "tampered worktree", worktree, last_run)

        if await self.git.is_dirty(location):
            return _skip(path, "uncommitted changes", worktree, last_run)

        if not _has_merged_pr(candidate):
            if worktree.base_sha is None:
                return _skip(path, "missing base sha", worktree, last_run)
            try:
                commits = await self.git.log(location, rev=f"{worktree.base_sha}..HEAD")
            except GitError:
                return _skip(path, "unmerged check failed", worktree, last_run)
            if commits:
                return _skip(path, "unmerged commits", worktree, last_run)

        return CleanupItem(worktree=worktree, path=path, last_run=last_run)

    # ---------------------------------------------------------------- apply --
    async def apply(self, plan: CleanupPlan) -> CleanupResult:
        """Remove exactly what ``plan`` returned, skipping anything that changed.

        Worktree removal always passes ``force=False``; a worktree that became
        dirty since planning is refused by :class:`WorktreeService` and reported
        as a skip. Scratch directories are re-checked against the runs root and
        for symlinks immediately before ``shutil.rmtree``.
        """
        worktrees_removed = 0
        scratch_dirs_removed = 0
        skips: list[CleanupSkip] = []

        for item in plan.worktrees:
            skip = await self._apply_worktree(item)
            if skip is None:
                worktrees_removed += 1
            else:
                skips.append(skip)

        for path in plan.scratch_dirs:
            skip = await self._apply_scratch(path)
            if skip is None:
                scratch_dirs_removed += 1
            else:
                skips.append(skip)

        return CleanupResult(
            worktrees_removed=worktrees_removed,
            scratch_dirs_removed=scratch_dirs_removed,
            skips=skips,
        )

    async def _apply_worktree(self, item: CleanupItem) -> CleanupSkip | None:
        """Remove one planned worktree, or return a skip when removal is refused."""
        try:
            async with session_scope(self.sessions) as session:
                service = WorktreeService(
                    git=self.git,
                    session=session,
                    workspace_id=item.worktree.workspace_id,
                )
                await service.remove(item.worktree, force=False)
                await AuditLogRepo(session, AuditLog, item.worktree.workspace_id).append(
                    actor="system",
                    action="cleanup.worktree",
                    target=str(item.path),
                    details_json={
                        "worktree_id": item.worktree.id,
                        "branch": item.worktree.branch,
                    },
                )
        except WorktreeDirty as exc:
            logger.warning(
                "cleanup.worktree_skipped",
                worktree_id=item.worktree.id,
                reason="uncommitted changes",
                path=str(exc.path),
            )
            return CleanupSkip(
                path=exc.path,
                reason="uncommitted changes",
                worktree=item.worktree,
                last_run=item.last_run,
            )
        except WorktreeTampered:
            logger.warning(
                "cleanup.worktree_skipped",
                worktree_id=item.worktree.id,
                reason="tampered worktree",
                path=str(item.path),
            )
            return CleanupSkip(
                path=item.path,
                reason="tampered worktree",
                worktree=item.worktree,
                last_run=item.last_run,
            )
        except UnsafePath:
            logger.warning(
                "cleanup.worktree_skipped",
                worktree_id=item.worktree.id,
                reason="unsafe path",
                path=str(item.path),
            )
            return CleanupSkip(
                path=item.path,
                reason="unsafe path",
                worktree=item.worktree,
                last_run=item.last_run,
            )

        logger.info(
            "cleanup.worktree_removed",
            worktree_id=item.worktree.id,
            branch=item.worktree.branch,
            path=str(item.path),
        )
        return None

    async def _apply_scratch(self, path: Path) -> CleanupSkip | None:
        """Remove one planned scratch directory, or return a skip."""
        if _is_symlink(path):
            return CleanupSkip(path=path, reason="symlink")
        try:
            resolved = resolve_within(self.settings.data_dir / "runs", path)
        except UnsafePath:
            return CleanupSkip(path=path, reason="unsafe path")

        async with session_scope(self.sessions) as session:
            run = await RunRepo(session).get_by_id(path.name)
            if run is None:
                return CleanupSkip(path=path, reason="run missing")
            shutil.rmtree(resolved)
            await AuditLogRepo(session, AuditLog, run.workspace_id).append(
                actor="system",
                action="cleanup.scratch",
                target=str(resolved),
                details_json={"run_id": run.id},
            )

        logger.info("cleanup.scratch_removed", run_id=path.name, path=str(resolved))
        return None

    # ------------------------------------------------------------ background --
    async def run_forever(self) -> None:
        """Run :meth:`plan` then :meth:`apply` on an interval until cancelled.

        An unexpected exception is logged and the loop continues after the
        interval; :class:`asyncio.CancelledError` propagates, which is how the
        API lifespan stops this task.
        """
        while True:
            try:
                plan = await self.plan(older_than_days=self.settings.cleanup_older_than_days)
                result = await self.apply(plan)
                logger.info(
                    "cleanup.round_finished",
                    worktrees_removed=result.worktrees_removed,
                    scratch_dirs_removed=result.scratch_dirs_removed,
                    skips=len(result.skips),
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("cleanup.round_failed")
            await self._sleep(self.settings.cleanup_interval_s)


def _skip(
    path: Path,
    reason: str,
    worktree: Worktree | None,
    last_run: Run | None,
) -> CleanupSkip:
    """Build a :class:`CleanupSkip` with the worktree context attached."""
    return CleanupSkip(path=path, reason=reason, worktree=worktree, last_run=last_run)


def _is_symlink(path: Path) -> bool:
    """``Path.is_symlink`` outside a coroutine (ruff ASYNC240)."""
    return path.is_symlink()


def _newest_run(runs: tuple[Run, ...]) -> Run:
    """Return the newest run by UUIDv7 id (creation order)."""
    return max(runs, key=lambda run: run.id)


def _has_merged_pr(candidate: CleanupWorktreeCandidate) -> bool:
    """True when any run on this worktree has a merged pull request."""
    return any(run.id in candidate.merged_pr_run_ids for run in candidate.runs)


def _scratch_dirs(root: Path, run_ids: set[str]) -> list[Path]:
    """Directories directly under ``root`` whose name is an old terminal run id.

    Plain function because ``os.scandir`` is blocking, and ``follow_symlinks``
    is ``False`` so a planted symlink is never followed.
    """
    if not root.is_dir():
        return []
    found: list[Path] = []
    with os.scandir(root) as entries:
        for entry in entries:
            if not entry.is_dir(follow_symlinks=False):
                continue
            if entry.name not in run_ids:
                continue
            found.append(Path(entry.path))
    return sorted(found)
