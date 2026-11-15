"""The real :class:`~gaia.orchestrator.deps.CheckRunner` (doc 12b-1).

The step's test command runs through the injected :class:`CommandRunner`, which
in production is :class:`~gaia.tools.shell.SrtCommandRunner` with **no
network**. The command policy is still evaluated first as defence in depth:
the command was already validated at plan time (doc 11), but checks never ask
a human, so ``ask`` is treated as a failure.
"""

from __future__ import annotations

import shlex
import shutil
from dataclasses import dataclass
from pathlib import Path

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from gaia.config.settings import Settings
from gaia.db.engine import session_scope
from gaia.db.ids import uuid7
from gaia.db.models import Repository, Run, RunStep, Worktree
from gaia.db.repo import RunRepo, ScopedRepo
from gaia.orchestrator.deps import CheckOutcome
from gaia.security.commands import CommandPolicy
from gaia.services.git import resolve_within
from gaia.services.proc import ProcResult
from gaia.tools.shell import CommandRunner

logger = structlog.get_logger(__name__)

#: Doc 11's cap for test output stored on a review or a PR body.
TEST_OUTPUT_CAP = 20_000


@dataclass(frozen=True)
class _CheckContext:
    """The rows one check needs, loaded in one short session."""

    run: Run
    step: RunStep
    repository: Repository
    worktree: Worktree


class SandboxCheckRunner:
    """Run a step's test command in the sandbox."""

    def __init__(
        self,
        *,
        settings: Settings,
        sessions: async_sessionmaker[AsyncSession],
        commands: CommandRunner,
        policy: CommandPolicy,
    ) -> None:
        self.settings = settings
        self.sessions = sessions
        self.commands = commands
        self.policy = policy

    async def run(self, run_id: str, step_index: int) -> CheckOutcome:
        """Return whether the step's checks passed."""
        context = await self._load_context(run_id, step_index)
        argv = _step_command(context.repository, context.step)
        if not argv:
            logger.info(
                "checks.no_command",
                run_id=run_id,
                step_index=step_index,
            )
            return CheckOutcome(passed=True, output="no test command configured")

        decision = self.policy.evaluate(
            argv,
            extra_allow=_repo_command(context.repository),
        )
        if decision.action in ("deny", "ask"):
            return CheckOutcome(
                passed=False,
                output=f"check command not allowed: {decision.reason}",
            )

        worktree = resolve_within(
            self.settings.worktrees_root,
            Path(context.worktree.path),
        )
        scratch = self._scratch_dir(run_id, step_index)
        try:
            result = await self.commands.run(
                argv,
                cwd=worktree,
                timeout_s=self.settings.check_timeout_s,
                task_dir=scratch,
            )
        finally:
            shutil.rmtree(scratch, ignore_errors=True)

        passed = result.returncode == 0 and not result.timed_out
        output = _format_output(result, timeout_s=self.settings.check_timeout_s)
        await self._persist_result(
            context.run.workspace_id,
            context.step.id,
            argv,
            passed=passed,
            returncode=result.returncode,
        )
        return CheckOutcome(passed=passed, output=output)

    async def _load_context(self, run_id: str, step_index: int) -> _CheckContext:
        """Load the run, step, repository and worktree rows in one short session."""
        async with session_scope(self.sessions) as session:
            run = await RunRepo(session).get_by_id(run_id)
            if run is None:
                raise ValueError(f"no run {run_id!r}")
            workspace_id = run.workspace_id
            steps = ScopedRepo(session, RunStep, workspace_id)
            step = next(
                iter(await steps.list(run_id=run_id, step_index=step_index, limit=1)),
                None,
            )
            if step is None:
                raise ValueError(f"no step {step_index!r} for run {run_id!r}")
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
            return _CheckContext(
                run=run,
                step=step,
                repository=repository,
                worktree=worktree,
            )

    async def _persist_result(
        self,
        workspace_id: str,
        step_id: str,
        argv: tuple[str, ...],
        *,
        passed: bool,
        returncode: int,
    ) -> None:
        """Store the short result summary 15a's PR body reads."""
        joined = shlex.join(argv)
        prefix = "passed: " if passed else f"failed (exit {returncode}): "
        summary = f"{prefix}{joined}"[:500]
        async with session_scope(self.sessions) as session:
            await ScopedRepo(session, RunStep, workspace_id).update(step_id, result_summary=summary)

    def _scratch_dir(self, run_id: str, step_index: int) -> Path:
        """A fresh, per-check scratch directory under Gaia's data dir."""
        path = self.settings.data_dir / "runs" / run_id / "checks" / str(step_index) / uuid7()
        path.mkdir(parents=True, exist_ok=True)
        return path


def _step_command(repository: Repository, step: RunStep) -> tuple[str, ...]:
    """The step's test command, or the repository's, as a tuple."""
    spec = step.spec_json
    if isinstance(spec, dict):
        command = spec.get("test_command")
        if isinstance(command, list):
            argv = _argv_tuple(command)
            if argv:
                return argv
    return _argv_tuple(repository.test_command)


def _repo_command(repository: Repository) -> tuple[tuple[str, ...], ...]:
    """The owner-registered repository test command for ``extra_allow``."""
    argv = _argv_tuple(repository.test_command)
    return (argv,) if argv else ()


def _argv_tuple(command: list[str] | None) -> tuple[str, ...]:
    """Return ``command`` as a tuple, ignoring a malformed registration."""
    if not command:
        return ()
    if not all(isinstance(part, str) for part in command):
        return ()
    return tuple(command)


def _format_output(result: ProcResult, *, timeout_s: float) -> str:
    """The check output, capped at :data:`TEST_OUTPUT_CAP` characters."""
    first = f"timed out after {timeout_s:g}s" if result.timed_out else f"exit={result.returncode}"
    parts = [first, "--- stdout ---"]
    if result.stdout:
        parts.append(result.stdout)
    if result.stdout_truncated:
        parts.append("[output truncated]")
    parts.append("--- stderr ---")
    if result.stderr:
        parts.append(result.stderr)
    if result.stderr_truncated:
        parts.append("[output truncated]")
    return "\n".join(parts)[-TEST_OUTPUT_CAP:]
