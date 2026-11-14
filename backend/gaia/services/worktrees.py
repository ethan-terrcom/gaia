"""The lifecycle of a run's worktree (doc 05 task 3).

A worktree is where an agent works: one per ticket, inside
``Settings.worktrees_root``, on a ``gaia/...`` branch. This module owns the two
things that are easy to get wrong:

- **The row and the directory agree.** The row is written first, in
  ``preparing``, so a crash mid-creation leaves a row that says so rather than an
  undocumented directory; it becomes ``ready`` only once ``git worktree add``
  returned, and ``failed`` if it did not.
- **Nothing escapes the root.** Every path is the ``worktrees_root`` joined with
  ids Gaia generated, resolved, and checked to still be under the root
  (:func:`gaia.services.git.resolve_within`). No caller supplies a path segment.

``list`` is the honest one: it reconciles the rows against
``git worktree list`` and reports both directions - rows git does not know
(orphans) and worktrees no row claims (``unknown_paths``) - because the only way
to keep a database and a directory tree in step is to measure the difference
rather than assume it is empty.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from gaia.db.ids import uuid7
from gaia.db.models import Repository, Worktree
from gaia.db.repo import ScopedRepo, WorkspaceMismatch
from gaia.domain.enums import WorktreeStatus
from gaia.services.git import GitService, GitWorktree, TicketLike, resolve_within

logger = structlog.get_logger(__name__)

#: What an id may look like when it becomes a path segment. UUIDv7 strings pass;
#: anything with a slash or a ``..`` in it does not.
_SEGMENT = re.compile(r"^[0-9a-zA-Z-]{1,64}$")

#: The URL scheme VS Code registers for "open this file/folder" (task 3).
VSCODE_SCHEME = "vscode://file"


class WorktreeError(RuntimeError):
    """A worktree operation was refused."""


class WorktreeAlreadyExists(WorktreeError):
    """A live worktree already uses this branch; the caller should reuse it."""

    def __init__(self, worktree: Worktree) -> None:
        self.worktree = worktree
        super().__init__(
            f"worktree {worktree.id} is already {worktree.status} on branch "
            f"{worktree.branch!r} at {worktree.path!r}"
        )


class WorktreeDirty(WorktreeError):
    """The worktree has uncommitted changes and ``force`` was not set."""

    def __init__(self, worktree: Worktree, path: Path) -> None:
        self.worktree = worktree
        self.path = path
        super().__init__(
            f"worktree {worktree.id} at {str(path)!r} has uncommitted changes; "
            "remove it with force to discard them"
        )


@dataclass(frozen=True)
class WorktreeItem:
    """One row of the reconciliation in :meth:`WorktreeService.list`.

    There is no ``dirty`` flag on purpose: it costs one ``git status`` per row,
    and doc 06's route asks for it per worktree it shows instead
    (``GitService.is_dirty``), so listing a repository stays a fixed number of git
    calls rather than one per worktree. Doc 17 shows the result.
    """

    worktree: Worktree
    #: The path is listed by ``git worktree list``.
    in_git: bool
    #: The directory is on disk.
    exists: bool

    @property
    def orphan(self) -> bool:
        """True when the row has no worktree behind it (the doc's flag).

        An orphan is harmless and untidy - a worktree removed behind Gaia's back,
        or a row left by a crash - and it is what doc 17 shows so the owner can
        clean up.
        """
        return not self.in_git


@dataclass(frozen=True)
class WorktreeReport:
    """What a repository's worktrees look like, both sides of the truth."""

    items: tuple[WorktreeItem, ...]
    #: Directories git knows about that no row claims.
    unknown_paths: tuple[Path, ...]

    @property
    def orphans(self) -> tuple[WorktreeItem, ...]:
        """Rows whose worktree git does not know about."""
        return tuple(item for item in self.items if item.orphan)


class WorktreeService:
    """Create, remove and reconcile the worktrees of one workspace.

    Scoped like :class:`gaia.db.repo.ScopedRepo`: it is constructed with the
    ``workspace_id`` whose rows it may touch, and it refuses a row from anyone
    else's. It writes through the repository helpers, so no statement here builds
    its own ``where`` clause.
    """

    def __init__(self, *, git: GitService, session: AsyncSession, workspace_id: str) -> None:
        self.git = git
        self.session = session
        self.workspace_id = workspace_id
        self.worktrees: ScopedRepo[Worktree] = ScopedRepo(session, Worktree, workspace_id)
        self.repositories: ScopedRepo[Repository] = ScopedRepo(session, Repository, workspace_id)

    # --------------------------------------------------------------- create --
    async def create(
        self, repo: Repository, ticket: TicketLike, base_ref: str | None = None
    ) -> Worktree:
        """Create the worktree for ``ticket`` in ``repo``, at ``base_ref``.

        ``base_ref`` defaults to :meth:`GitService.default_branch`. The branch
        name comes from :meth:`GitService.branch_name_for`, so it is always
        ``gaia/...`` and never chosen by a client.

        Re-running the same ticket reuses its row: a worktree that was removed
        (``removed``) is brought back on the same branch, because a run resumed
        after cleanup must not fail on a stale row. A worktree that is still live
        raises :class:`WorktreeAlreadyExists`, whose ``.worktree`` is the one to
        reuse - the caller decides, the old worktree may hold uncommitted work.
        """
        self._check_workspace(repo)
        repo_path = await self.git.validate_repo(repo.local_path)
        reference = base_ref if base_ref is not None else await self.git.default_branch(repo_path)
        branch = await self.git.validate_ref(self.git.branch_name_for(ticket))
        base_sha = await self.git.current_sha(repo_path, rev=reference)

        row = await self._row_for(repo, branch, base_sha)
        path = resolve_within(self.git.worktrees_root, Path(row.path))
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            if await self.git.branch_exists(repo_path, branch):
                # A resumed run: check the branch out instead of recreating it.
                await self.git.worktree_add(repo_path, path=path, ref=branch)
            else:
                await self.git.worktree_add(repo_path, path=path, ref=reference, new_branch=branch)
        except BaseException:
            # A row that says ``failed`` is a bug report; a directory with no row
            # is a mystery. Record it before the error goes up.
            await self.worktrees.update(row.id, status=WorktreeStatus.FAILED.value)
            logger.warning("worktree.create_failed", worktree_id=row.id, branch=branch)
            raise
        logger.info("worktree.created", worktree_id=row.id, branch=branch, path=str(path))
        return await self.worktrees.update(row.id, status=WorktreeStatus.READY.value)

    async def _row_for(self, repo: Repository, branch: str, base_sha: str) -> Worktree:
        """The row to fill in for ``branch``: new, revived, or refused."""
        existing = await self.worktrees.list(repo_id=repo.id, branch=branch)
        if not existing:
            row_id = uuid7()
            return await self.worktrees.create(
                id=row_id,
                repo_id=repo.id,
                branch=branch,
                path=str(self.path_for(repo, row_id)),
                base_sha=base_sha,
                status=WorktreeStatus.PREPARING.value,
            )
        row = existing[0]
        if row.status != WorktreeStatus.REMOVED.value:
            raise WorktreeAlreadyExists(row)
        # Revive the old row rather than breaking the (repo_id, branch) unique
        # constraint with a second one for the same branch.
        return await self.worktrees.update(
            row.id, base_sha=base_sha, status=WorktreeStatus.PREPARING.value
        )

    def path_for(self, repo: Repository, worktree_id: str) -> Path:
        """``<worktrees_root>/<repo_id>/<worktree_id>``, confined (task 4)."""
        _check_segment(repo.id, "repository id")
        _check_segment(worktree_id, "worktree id")
        candidate = self.git.worktrees_root / repo.id / worktree_id
        return resolve_within(self.git.worktrees_root, candidate)

    # --------------------------------------------------------------- remove --
    async def remove(self, worktree: Worktree, *, force: bool = False) -> Worktree:
        """Remove ``worktree`` from disk and mark its row ``removed``.

        Refused while the tree is dirty unless ``force`` (task 3): uncommitted
        work is the one thing a worktree exists to hold, and discarding it by
        accident costs a run. The row is kept - history of where a run worked is
        worth more than a tidy table - and ``git worktree prune`` runs afterwards
        so the repo's own bookkeeping matches.
        """
        self._check_workspace(worktree)
        if worktree.status == WorktreeStatus.REMOVED.value:
            return worktree
        path = resolve_within(self.git.worktrees_root, Path(worktree.path))
        repo = await self.repositories.get(worktree.repo_id)
        if repo is None:
            raise WorktreeError(
                f"worktree {worktree.id} points at repository {worktree.repo_id!r}, "
                "which is not in this workspace"
            )
        repo_path = await self.git.validate_repo(repo.local_path)
        if path.exists():
            if not force and await self.git.is_dirty(path):
                raise WorktreeDirty(worktree, path)
            await self.git.worktree_remove(repo_path, path, force=force)
        # Also handles a directory that was deleted behind Gaia's back, which is
        # exactly what `git worktree remove` refuses to do.
        await self.git.worktree_prune(repo_path)
        logger.info("worktree.removed", worktree_id=worktree.id, force=force, path=str(path))
        return await self.worktrees.update(worktree.id, status=WorktreeStatus.REMOVED.value)

    # ----------------------------------------------------------------- list --
    async def list(self, repo: Repository, *, include_removed: bool = False) -> WorktreeReport:
        """Reconcile this repo's rows against ``git worktree list`` (task 3).

        Both sides are reported: a row with no worktree is an *orphan*, and a
        worktree with no row is in ``unknown_paths``. Neither is an error - a
        worktree removed in the owner's terminal is a normal thing to find - so
        the caller decides what to show or clean up.

        ``removed`` rows are left out by default: a removed worktree is history of
        where a run worked, not something the repository currently has, and doc 06
        lists a repository's worktrees. Pass ``include_removed=True`` for the audit
        view; the rows are still in the table either way (:meth:`remove` keeps
        them, and ``create`` revives one rather than inserting a second).

        Deliberately *not* here: a per-row ``dirty`` flag. It costs one
        ``git status`` per worktree, and this is called behind one HTTP request
        (doc 06), so the route asks for it per worktree it actually shows:

        TODO(gaia-06): add ``dirty`` from ``GitService.is_dirty(item.path)`` in
        the worktree route, not here.
        """
        self._check_workspace(repo)
        rows = await self.worktrees.list(repo_id=repo.id)
        if not include_removed:
            rows = [row for row in rows if row.status != WorktreeStatus.REMOVED.value]
        repo_path = await self.git.validate_repo(repo.local_path)
        entries = await self.git.worktree_list(repo_path)
        return _reconcile(rows, entries, repo_path)

    # -------------------------------------------------------------- editors --
    def open_in_editor(self, worktree: Worktree) -> str:
        """A ``vscode://file/<path>`` URL for the UI to open (task 3).

        A string, never an action: Gaia does not launch an editor, and a deep link
        is the UI's business (doc 17). The path is quoted, so a worktree directory
        with a space in it survives the trip through a URL.
        """
        self._check_workspace(worktree)
        path = resolve_within(self.git.worktrees_root, Path(worktree.path))
        return f"{VSCODE_SCHEME}{quote(str(path))}"

    def _check_workspace(self, row: Worktree | Repository) -> None:
        """Refuse a row that belongs to another workspace."""
        if row.workspace_id != self.workspace_id:
            raise WorkspaceMismatch(type(row).__name__, self.workspace_id, row.workspace_id)


def _check_segment(value: str, what: str) -> None:
    """Refuse an id that is not safe as a single path segment.

    The ids are Gaia's own UUIDv7 strings, so this never fires in practice. It is
    here because ``repo.id`` arrives from a database row, and a row is not a
    place to trust for a filesystem path.
    """
    if not _SEGMENT.match(value):
        raise WorktreeError(f"{what} {value!r} is not usable as a directory name")


def _reconcile(rows: list[Worktree], entries: list[GitWorktree], repo_path: Path) -> WorktreeReport:
    """Compare the rows with what git reports, in both directions.

    The repository's own working tree is in ``git worktree list`` too, and it is
    not a run's worktree, so it is dropped from ``unknown_paths``.

    A plain function because resolving and stat-ing paths is blocking, and ruff's
    ASYNC240 keeps that out of coroutines.
    """
    root = repo_path.resolve()
    known = {entry.path.resolve() for entry in entries if entry.path.resolve() != root}
    row_paths = {Path(row.path).resolve() for row in rows}
    items = tuple(
        WorktreeItem(
            worktree=row,
            in_git=Path(row.path).resolve() in known,
            exists=Path(row.path).exists(),
        )
        for row in rows
    )
    return WorktreeReport(
        items=items,
        unknown_paths=tuple(sorted(path for path in known if path not in row_paths)),
    )
