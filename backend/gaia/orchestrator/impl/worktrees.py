"""The real :class:`~gaia.orchestrator.deps.WorktreePreparer` (doc 12b-1).

It creates or reuses the run's worktree and, when the repository owner
registered one, runs the setup command before any agent touches the checkout.
The setup command is **not** policy-checked: it is the owner's own registered
command, run in the sandbox before the worktree is handed to an agent.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from gaia.config.settings import Settings
from gaia.db.engine import session_scope
from gaia.db.models import Repository, Run, Ticket, Worktree
from gaia.db.repo import RunRepo, ScopedRepo
from gaia.domain.enums import WorktreeStatus
from gaia.orchestrator.impl import worktree_location
from gaia.security.redaction import redact
from gaia.services.git import GitService, WorktreeLocation
from gaia.services.proc import ProcResult
from gaia.services.worktrees import WorktreeAlreadyExists, WorktreeError, WorktreeService
from gaia.tools.shell import CommandRunner

#: The setup command's combined output is capped at this many characters.
_SETUP_OUTPUT_TAIL_CHARS = 2_000


_SETUP_ERROR_CHARS = 800


class SetupFailed(RuntimeError):
    """The repository's owner-registered setup command did not succeed."""

    def __init__(self, returncode: int, tail: str) -> None:
        self.returncode = returncode
        self.tail = tail
        # The tail is already redacted. It goes in the message so the run's failure
        # event and the log say *why*, not just the exit status.
        detail = f":\n{tail.strip()[-_SETUP_ERROR_CHARS:]}" if tail.strip() else ""
        super().__init__(f"setup command exited with status {returncode}{detail}")


@dataclass(frozen=True)
class _RunContext:
    """The rows :meth:`WorktreeSetup.prepare` needs, loaded in one short session."""

    run: Run
    ticket: Ticket
    repository: Repository
    worktree: Worktree | None


class WorktreeSetup:
    """Create or reuse a run's worktree, then run the owner's setup command.

    ``commands`` is the injected :class:`CommandRunner` built for setup by the
    composition root (12b-3): it carries :attr:`Settings.setup_allowed_domains`
    as its network allowlist.
    """

    def __init__(
        self,
        *,
        settings: Settings,
        sessions: async_sessionmaker[AsyncSession],
        git: GitService,
        commands: CommandRunner,
    ) -> None:
        self.settings = settings
        self.sessions = sessions
        self.git = git
        self.commands = commands

    async def prepare(self, run_id: str) -> str:
        """Return the worktree id for ``run_id``, creating it when needed.

        The method is idempotent: a resumed node calls it again after a crash.
        The setup marker lives under ``<data_dir>/runs/<run_id>/setup.ok``,
        outside the worktree, so an agent cannot fake a successful setup.
        """
        context = await self._load_context(run_id)
        worktree = await self._reuse_or_create(context)
        location = await self._location_for(context.repository, worktree)
        # Refuse a tampered worktree before any setup command runs in it.
        self.git.verify_location(location)

        if self._setup_marker(run_id).exists():
            return worktree.id

        await self._mark_in_use(context.run.workspace_id, worktree.id)
        await self._run_setup(context, worktree, location)
        return worktree.id

    async def _load_context(self, run_id: str) -> _RunContext:
        """Load the run, ticket, repository and current worktree row."""
        async with session_scope(self.sessions) as session:
            run = await RunRepo(session).get_by_id(run_id)
            if run is None:
                raise ValueError(f"no run {run_id!r}")
            workspace_id = run.workspace_id
            tickets = ScopedRepo(session, Ticket, workspace_id)
            ticket = await tickets.get(run.ticket_id)
            if ticket is None:
                raise ValueError(f"no ticket {run.ticket_id!r} in workspace {workspace_id!r}")
            repositories = ScopedRepo(session, Repository, workspace_id)
            repository = await repositories.get(run.repo_id)
            if repository is None:
                raise ValueError(f"no repository {run.repo_id!r} in workspace {workspace_id!r}")
            worktree = None
            if run.worktree_id is not None:
                worktrees = ScopedRepo(session, Worktree, workspace_id)
                worktree = await worktrees.get(run.worktree_id)
                if worktree is not None and worktree.repo_id != run.repo_id:
                    raise WorktreeError(
                        f"run {run_id!r} points at worktree {worktree.id!r} "
                        f"for repository {worktree.repo_id!r}, not {run.repo_id!r}"
                    )
            return _RunContext(
                run=run,
                ticket=ticket,
                repository=repository,
                worktree=worktree,
            )

    async def _reuse_or_create(self, context: _RunContext) -> Worktree:
        """Return the live worktree row, reusing it or asking the service to create it."""
        existing = context.worktree
        if existing is not None and existing.status in (
            WorktreeStatus.READY.value,
            WorktreeStatus.IN_USE.value,
        ):
            return existing
        try:
            async with session_scope(self.sessions) as session:
                service = WorktreeService(
                    git=self.git,
                    session=session,
                    workspace_id=context.run.workspace_id,
                )
                return await service.create(context.repository, context.ticket)
        except WorktreeAlreadyExists as exc:
            if exc.status not in (
                WorktreeStatus.READY.value,
                WorktreeStatus.IN_USE.value,
            ):
                raise
            # ``exc.worktree`` was expired by the rollback that raising caused:
            # read the live row again in a session of its own.
            async with session_scope(self.sessions) as session:
                live = await ScopedRepo(session, Worktree, context.run.workspace_id).get(
                    exc.worktree_id
                )
                if live is None:
                    raise
                # The live worktree belongs to an earlier run on this ticket. If
                # that run left no work in it, move it up to today's base so the
                # re-run does not set up and work on stale code.
                service = WorktreeService(
                    git=self.git,
                    session=session,
                    workspace_id=context.run.workspace_id,
                )
                return await service.refresh(live, context.repository)

    async def _mark_in_use(self, workspace_id: str, worktree_id: str) -> None:
        """Move a ``ready`` worktree to ``in_use`` before the setup command.

        ``WorktreeStatus`` has no central transition machine in
        :mod:`gaia.domain.state` (doc 05 owns the worktree lifecycle), so the
        legal ``ready → in_use`` move is made here, exactly like
        :class:`~gaia.services.worktrees.WorktreeService` writes its other
        statuses directly.
        """
        async with session_scope(self.sessions) as session:
            worktrees = ScopedRepo(session, Worktree, workspace_id)
            worktree = await worktrees.get(worktree_id)
            if worktree is None:
                raise WorktreeError(f"no worktree {worktree_id!r} in workspace {workspace_id!r}")
            if worktree.status == WorktreeStatus.READY.value:
                worktree.status = WorktreeStatus.IN_USE.value
            elif worktree.status != WorktreeStatus.IN_USE.value:
                raise WorktreeError(
                    f"cannot mark worktree {worktree_id!r} in_use from {worktree.status!r}"
                )

    async def _run_setup(
        self,
        context: _RunContext,
        worktree: Worktree,
        location: WorktreeLocation,
    ) -> None:
        """Run the owner's setup command once, then write the marker."""
        marker = self._setup_marker(context.run.id)
        command = _setup_argv(context.repository.setup_command)
        if not command:
            _write_marker(marker)
            return

        task_dir = marker.parent / "setup-tmp"
        task_dir.mkdir(parents=True, exist_ok=True)
        result = await self.commands.run(
            command,
            cwd=location.work_tree,
            timeout_s=self.settings.setup_timeout_s,
            task_dir=task_dir,
        )
        if not result.ok:
            raise SetupFailed(result.returncode, _setup_output_tail(result))
        _write_marker(marker)

    async def _location_for(self, repository: Repository, worktree: Worktree) -> WorktreeLocation:
        """Build the pinned location without holding a database session."""
        repo_path = await self.git.validate_repo(repository.local_path)
        return await worktree_location(git=self.git, repo_path=repo_path, worktree=worktree)

    def _setup_marker(self, run_id: str) -> Path:
        """The marker that proves setup already ran for ``run_id``."""
        return self.settings.data_dir / "runs" / run_id / "setup.ok"


def _setup_argv(command: list[str] | None) -> tuple[str, ...]:
    """Return ``command`` as a tuple, dropping an empty or malformed registration."""
    if not command:
        return ()
    if not all(isinstance(part, str) for part in command):
        raise WorktreeError("repository.setup_command must be a list of strings")
    return tuple(command)


def _setup_output_tail(result: ProcResult) -> str:
    """The last 2,000 characters of combined setup output, redacted."""
    combined = redact(f"{result.stdout}\n{result.stderr}")
    return combined[-_SETUP_OUTPUT_TAIL_CHARS:]


def _write_marker(marker: Path) -> None:
    """Write ``setup.ok`` under the run's data directory."""
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text("ok\n", encoding="utf-8")
