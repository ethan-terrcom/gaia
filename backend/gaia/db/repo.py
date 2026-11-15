"""Workspace-scoped repositories: the only way to query a tenant table.

Owned by doc 03 tasks 5 and 6. ARCHITECTURE section 7 says the ``workspace_id``
filter lives in a repository helper "never ad hoc", so:

- :class:`ScopedRepo` builds every statement with ``workspace_id = <the repo's>``
  baked in. A row from another workspace is invisible: :meth:`ScopedRepo.get`
  returns ``None`` and :meth:`ScopedRepo.update` raises, rather than leaking the
  existence of someone else's data.
- :class:`AppendOnlyRepo` is the same idea with a smaller surface. ``event`` and
  ``audit_log`` get ``append`` and ``list`` and nothing else, because a trail
  that can be rewritten is not a trail.
- Nothing outside ``gaia/db/`` should call ``select()`` on a tenant table. The
  filter names :meth:`ScopedRepo.list` takes are column names chosen by a
  service, never a string from a request or from a model.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any, cast

from sqlalchemy import Select, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from gaia.db.models import (
    Approval,
    AuditLog,
    Base,
    Event,
    LlmUsage,
    PullRequest,
    Repository,
    Run,
    RunStep,
    RunToken,
    Ticket,
    Worktree,
)
from gaia.db.types import utcnow
from gaia.domain.enums import (
    ApprovalKind,
    ApprovalStatus,
    PullRequestState,
    RunStatus,
    RunStepStatus,
    WorktreeStatus,
)


class WorkspaceMismatch(ValueError):
    """A write tried to put a row in a workspace the repo does not own."""

    def __init__(self, model: str, expected: str, given: str) -> None:
        self.model = model
        self.expected = expected
        self.given = given
        super().__init__(
            f"cannot write {model} into workspace {given!r} from a repo scoped to {expected!r}"
        )


class NotFound(LookupError):
    """No row with that id exists in this workspace."""

    def __init__(self, model: str, row_id: str) -> None:
        self.model = model
        self.row_id = row_id
        super().__init__(f"no {model} with id {row_id!r} in this workspace")


def _workspace_of(row: Any) -> str:
    """Read a row's tenant id.

    ``row`` is typed ``Any`` on purpose: every model gets ``workspace_id`` from
    :class:`gaia.db.models.WorkspaceScoped`, but the repository is generic over
    the model class, and mypy cannot see a mixin's column through a ``TypeVar``.
    """
    workspace_id: str = row.workspace_id
    return workspace_id


class RunTokenRepo:
    """Unscoped run-token lookups and idempotent revocation (doc 07b).

    Resolution deliberately does **not** filter by workspace: the SHA-256 token
    hash is globally unique, and the token row itself is what proves the
    workspace. ``revoke_*`` only sets ``revoked_at`` on live tokens, so calling
    either method twice is a no-op.
    """

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get(self, token_id: str) -> RunToken | None:
        """Return a token by id, for tests and administrative revocation."""
        return await self.session.get(RunToken, token_id)

    async def get_by_hash(self, token_hash: str) -> RunToken | None:
        """Return the token whose hash matches, if any (no workspace filter)."""
        statement = select(RunToken).where(RunToken.token_hash == token_hash)
        return (await self.session.scalars(statement)).one_or_none()

    async def revoke_by_id(self, workspace_id: str, token_id: str) -> None:
        """Revoke one token by id, leaving an already-revoked token untouched.

        Scoped by workspace like every other write (review 01-05, F5): a token
        id from another workspace is a no-op, not a revocation.
        """
        statement = (
            update(RunToken)
            .where(
                RunToken.workspace_id == workspace_id,
                RunToken.id == token_id,
                RunToken.revoked_at.is_(None),
            )
            .values(revoked_at=utcnow())
        )
        await self.session.execute(statement)

    async def revoke_for_run(self, workspace_id: str, run_id: str) -> None:
        """Revoke every live token minted for one run in one workspace."""
        statement = (
            update(RunToken)
            .where(
                RunToken.workspace_id == workspace_id,
                RunToken.run_id == run_id,
                RunToken.revoked_at.is_(None),
            )
            .values(revoked_at=utcnow())
        )
        await self.session.execute(statement)


class RunRepo:
    """System-level run lookups used by the orchestrator runner.

    Normal tenant reads go through :class:`ScopedRepo`; the runner needs two
    process-wide queries (find runs to recover at startup, and load a run to
    cancel without a workspace argument). They live here so the orchestrator
    never builds SQL itself.
    """

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_by_id(self, run_id: str) -> Run | None:
        """Return any run by id, or ``None``."""
        return await self.session.get(Run, run_id)

    async def list_recoverable(self) -> list[Run]:
        """Return runs that may need to be resumed after a process restart.

        Terminal runs and runs already sitting at a human gate are left alone:
        an interrupted run already has a pending checkpoint question, a
        ``paused_budget`` run has a budget question, and a ``paused`` run is
        only resumed by an explicit resume-all (the kill switch, doc 18a).
        """
        ignored = (
            RunStatus.FAILED.value,
            RunStatus.COMPLETED.value,
            RunStatus.CANCELLED.value,
            RunStatus.AWAITING_APPROVAL.value,
            RunStatus.PAUSED_BUDGET.value,
            RunStatus.PAUSED.value,
        )
        statement = select(Run).where(~Run.status.in_(ignored)).order_by(Run.id)
        return list((await self.session.scalars(statement)).all())

    async def list_by_status(self, status: str) -> list[Run]:
        """Return every run in ``status``, process-wide.

        Used by the kill switch to find the runs ``resume_all`` must relaunch.
        Like the other system-level lookups here, it is deliberately unscoped.
        """
        statement = select(Run).where(Run.status == status).order_by(Run.id)
        return list((await self.session.scalars(statement)).all())

    async def list_summary(
        self,
        workspace_id: str,
        *,
        status: str | None,
        after_id: str | None,
        limit: int,
    ) -> list[RunSummaryRow]:
        """Return one workspace's runs, newest first, with their dashboard fields.

        One SQL statement only: the ticket is joined and the current step and PR
        fields are correlated scalar subqueries, so the page limit applies to
        runs rather than to joined child rows.
        """
        current_step_index = (
            select(RunStep.step_index)
            .where(
                RunStep.run_id == Run.id,
                RunStep.status != RunStepStatus.COMPLETED.value,
            )
            .order_by(RunStep.step_index)
            .limit(1)
            .correlate(Run)
            .scalar_subquery()
        )
        current_step_title = (
            select(RunStep.title)
            .where(
                RunStep.run_id == Run.id,
                RunStep.status != RunStepStatus.COMPLETED.value,
            )
            .order_by(RunStep.step_index)
            .limit(1)
            .correlate(Run)
            .scalar_subquery()
        )
        latest_pr = (
            select(PullRequest.id)
            .where(PullRequest.run_id == Run.id)
            .order_by(PullRequest.created_at.desc(), PullRequest.id.desc())
            .limit(1)
            .correlate(Run)
            .scalar_subquery()
        )
        pr_number = (
            select(PullRequest.number)
            .where(PullRequest.id == latest_pr)
            .correlate(Run)
            .scalar_subquery()
        )
        pr_url = (
            select(PullRequest.url)
            .where(PullRequest.id == latest_pr)
            .correlate(Run)
            .scalar_subquery()
        )
        pr_ci_status = (
            select(PullRequest.ci_status)
            .where(PullRequest.id == latest_pr)
            .correlate(Run)
            .scalar_subquery()
        )
        statement = (
            select(
                Run,
                Ticket,
                current_step_index.label("current_step_index"),
                current_step_title.label("current_step_title"),
                pr_number.label("pr_number"),
                pr_url.label("pr_url"),
                pr_ci_status.label("pr_ci_status"),
            )
            .join(Ticket, Ticket.id == Run.ticket_id)
            .where(
                Run.workspace_id == workspace_id,
                Ticket.workspace_id == workspace_id,
            )
        )
        if status is not None:
            statement = statement.where(Run.status == status)
        if after_id is not None:
            statement = statement.where(Run.id < after_id)
        statement = statement.order_by(Run.id.desc()).limit(limit)
        rows = (await self.session.execute(statement)).all()
        return [
            RunSummaryRow(
                run=run,
                ticket=ticket,
                current_step_index=current_step_index_value,
                current_step_title=current_step_title_value,
                pr_number=pr_number_value,
                pr_url=pr_url_value,
                pr_ci_status=pr_ci_status_value,
            )
            for (
                run,
                ticket,
                current_step_index_value,
                current_step_title_value,
                pr_number_value,
                pr_url_value,
                pr_ci_status_value,
            ) in rows
        ]

    async def get_detail(self, workspace_id: str, run_id: str) -> RunDetailRow | None:
        """Return the rows behind ``GET /runs/{id}``, scoped to ``workspace_id``."""
        run = await ScopedRepo(self.session, Run, workspace_id).get(run_id)
        if run is None:
            return None
        ticket = await ScopedRepo(self.session, Ticket, workspace_id).get(run.ticket_id)
        if ticket is None:
            return None
        worktree = (
            await ScopedRepo(self.session, Worktree, workspace_id).get(run.worktree_id)
            if run.worktree_id is not None
            else None
        )
        steps = sorted(
            await ScopedRepo(self.session, RunStep, workspace_id).list(run_id=run_id),
            key=lambda step: step.step_index,
        )
        approvals = await ApprovalRepo(self.session, Approval, workspace_id).list_inbox(
            run_id=run_id
        )
        pull_requests = await ScopedRepo(self.session, PullRequest, workspace_id).list(
            run_id=run_id
        )
        pull_request = (
            max(pull_requests, key=lambda pr: (pr.created_at, pr.id)) if pull_requests else None
        )
        return RunDetailRow(
            run=run,
            ticket=ticket,
            worktree=worktree,
            steps=steps,
            approvals=approvals,
            pull_request=pull_request,
        )


@dataclass(frozen=True)
class PullRequestSyncRow:
    """One open PR with the run and repository the PR sync service needs."""

    pull_request: PullRequest
    run: Run
    repository: Repository


@dataclass(frozen=True)
class PullRequestWithContext:
    """One PR with the run, ticket and repository the API list route returns."""

    pull_request: PullRequest
    run: Run
    ticket: Ticket
    repository: Repository


@dataclass(frozen=True)
class ApprovalWaitStats:
    """Approval wait time over a window (doc 18b).

    Percentiles are computed in Python over the fetched durations; that is fine
    at this scale and keeps one percentile definition for SQLite and Postgres.
    """

    count: int
    avg_s: float
    p50_s: float
    p90_s: float


@dataclass(frozen=True)
class CleanupWorktreeCandidate:
    """One live worktree and the rows cleanup needs to decide its fate."""

    worktree: Worktree
    repository: Repository | None
    runs: tuple[Run, ...]
    merged_pr_run_ids: frozenset[str]


class CleanupRepo:
    """System-level cleanup queries used by doc 18c.

    Like :class:`RunRepo.get_by_id` and :class:`PullRequestRepo.list_open`, these
    are workspace-unscoped on purpose: cleanup runs across every workspace, so it
    must never be handed to a route.
    """

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def load_worktree_candidates(self) -> list[CleanupWorktreeCandidate]:
        """Return every live worktree with its runs, repository and merged PR ids."""
        live = (WorktreeStatus.READY.value, WorktreeStatus.IN_USE.value)
        worktrees = list(
            (
                await self.session.scalars(
                    select(Worktree).where(Worktree.status.in_(live)).order_by(Worktree.id)
                )
            ).all()
        )
        if not worktrees:
            return []

        repository_ids = {worktree.repo_id for worktree in worktrees}
        repositories = list(
            (
                await self.session.scalars(
                    select(Repository).where(Repository.id.in_(repository_ids))
                )
            ).all()
        )
        repositories_by_id = {repository.id: repository for repository in repositories}

        worktree_ids = [worktree.id for worktree in worktrees]
        runs = list(
            (
                await self.session.scalars(
                    select(Run).where(Run.worktree_id.in_(worktree_ids)).order_by(Run.id)
                )
            ).all()
        )
        runs_by_worktree: dict[str, list[Run]] = {}
        for run in runs:
            # ``where(Run.worktree_id.in_(...))`` makes this non-null; the cast
            # keeps mypy strict without an impossible branch.
            runs_by_worktree.setdefault(cast(str, run.worktree_id), []).append(run)

        merged_pr_run_ids: set[str] = set()
        run_ids = [run.id for run in runs]
        if run_ids:
            merged_pr_run_ids = set(
                (
                    await self.session.scalars(
                        select(PullRequest.run_id).where(
                            PullRequest.run_id.in_(run_ids),
                            PullRequest.state == PullRequestState.MERGED.value,
                        )
                    )
                ).all()
            )

        return [
            CleanupWorktreeCandidate(
                worktree=worktree,
                repository=repositories_by_id.get(worktree.repo_id),
                runs=tuple(runs_by_worktree.get(worktree.id, ())),
                merged_pr_run_ids=frozenset(merged_pr_run_ids),
            )
            for worktree in worktrees
        ]

    async def list_old_terminal_run_ids(self, cutoff: datetime) -> set[str]:
        """Return terminal run ids finished before ``cutoff`` for scratch cleanup."""
        terminal = (
            RunStatus.COMPLETED.value,
            RunStatus.FAILED.value,
            RunStatus.CANCELLED.value,
        )
        return set(
            (
                await self.session.scalars(
                    select(Run.id).where(
                        Run.status.in_(terminal),
                        Run.finished_at.is_not(None),
                        Run.finished_at < cutoff,
                    )
                )
            ).all()
        )


@dataclass(frozen=True)
class RunSummaryRow:
    """One row of the runs list: the run plus its dashboard fields."""

    run: Run
    ticket: Ticket
    current_step_index: int | None
    current_step_title: str | None
    pr_number: int | None
    pr_url: str | None
    pr_ci_status: str | None


@dataclass(frozen=True)
class RunDetailRow:
    """The rows backing ``GET /runs/{id}`` (doc 14)."""

    run: Run
    ticket: Ticket
    worktree: Worktree | None
    steps: list[RunStep]
    approvals: list[Approval]
    pull_request: PullRequest | None


class PullRequestRepo:
    """System-level PR queries used by doc 15b.

    ``list_open`` is workspace-unscoped on purpose: the background sync service
    has to refresh every open PR across every workspace. Like
    :class:`RunRepo.get_by_id`, it must never be handed to a route; route code
    goes through :class:`ScopedRepo`.
    """

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def list_open(self) -> list[PullRequestSyncRow]:
        """Return every open PR, joined to its run and repository."""
        statement = (
            select(PullRequest, Run, Repository)
            .join(Run, Run.id == PullRequest.run_id)
            .join(Repository, Repository.id == Run.repo_id)
            .where(PullRequest.state == PullRequestState.OPEN.value)
            .order_by(PullRequest.id)
        )
        rows = (await self.session.execute(statement)).all()
        return [
            PullRequestSyncRow(pull_request=pr, run=run, repository=repository)
            for pr, run, repository in rows
        ]

    async def list_with_context(
        self,
        workspace_id: str,
        state: str | None,
        limit: int,
    ) -> list[PullRequestWithContext]:
        """Return one workspace's PRs, newest first, with run/ticket/repository.

        One joined query only: the API route must never fall back to per-row
        lookups (doc 15b).
        """
        statement = (
            select(PullRequest, Run, Ticket, Repository)
            .join(Run, Run.id == PullRequest.run_id)
            .join(Ticket, Ticket.id == Run.ticket_id)
            .join(Repository, Repository.id == Run.repo_id)
            # Every joined row is scoped too, not only the PR: the foreign keys do
            # not yet enforce a shared workspace (review finding F5).
            .where(
                PullRequest.workspace_id == workspace_id,
                Run.workspace_id == workspace_id,
                Ticket.workspace_id == workspace_id,
                Repository.workspace_id == workspace_id,
            )
        )
        if state is not None:
            statement = statement.where(PullRequest.state == state)
        statement = statement.order_by(PullRequest.created_at.desc(), PullRequest.id.desc())
        statement = statement.limit(limit)
        rows = (await self.session.execute(statement)).all()
        return [
            PullRequestWithContext(
                pull_request=pr,
                run=run,
                ticket=ticket,
                repository=repository,
            )
            for pr, run, ticket, repository in rows
        ]


async def add_run_cost(session: AsyncSession, run_id: str, cost_usd: Decimal) -> None:
    """Add ``cost_usd`` to ``run.cost_usd`` atomically (doc 07b).

    The read-modify-write alternative would lose a concurrent usage row; the
    database does the addition instead.
    """
    statement = update(Run).where(Run.id == run_id).values(cost_usd=Run.cost_usd + cost_usd)
    await session.execute(statement)


async def daily_llm_usage_cost_usd(
    session: AsyncSession, workspace_id: str, *, start: datetime, end: datetime
) -> Decimal:
    """Return the sum of ``llm_usage.cost_usd`` in the half-open UTC day.

    The query lives here because the "never build SQL outside ``gaia/db``" rule
    applies just as much to aggregates as to row reads.
    """
    total = await session.scalar(
        select(func.coalesce(func.sum(LlmUsage.cost_usd), 0)).where(
            LlmUsage.workspace_id == workspace_id,
            LlmUsage.created_at >= start,
            LlmUsage.created_at < end,
        )
    )
    # SQLite returns a float for SUM over NUMERIC; go through str so the
    # Decimal is the value printed, not the float's binary expansion.
    return Decimal(str(total or 0))


async def daily_llm_usage_by_provider(
    session: AsyncSession, workspace_id: str, *, start: datetime, end: datetime
) -> list[tuple[str, Decimal]]:
    """Return ``(provider, cost_usd)`` rows for the half-open UTC day.

    Providers are sorted by name so the top-bar tooltip is deterministic. Like
    the daily total, SQLite may return SUM as a float, so every cost goes through
    ``str`` before it becomes a ``Decimal``.
    """
    statement = (
        select(LlmUsage.provider, func.coalesce(func.sum(LlmUsage.cost_usd), 0))
        .where(
            LlmUsage.workspace_id == workspace_id,
            LlmUsage.created_at >= start,
            LlmUsage.created_at < end,
        )
        .group_by(LlmUsage.provider)
        .order_by(LlmUsage.provider)
    )
    rows = (await session.execute(statement)).all()
    return [(str(provider), Decimal(str(cost or 0))) for provider, cost in rows]


async def count_decided_budget_approvals(
    session: AsyncSession,
    *,
    run_id: str,
    workspace_id: str,
    key_prefix: str,
) -> int:
    """Count decided budget approvals of one run whose key starts with ``key_prefix``.

    The guard's daily-budget interrupt keys look like
    ``budget:<node>:daily:<n>``. The next ``n`` is the number of rows that were
    already decided under that node prefix, so a paused node re-running on
    resume reuses its still-pending row (count 0), while a later daily pause
    gets a fresh key. A prefix match on the controlled ``idempotency_key`` is
    used instead of JSON-filtering ``payload_json["scope"]``: it stays portable
    across SQLite and PostgreSQL.
    """
    statement = (
        select(func.count())
        .select_from(Approval)
        .where(
            Approval.workspace_id == workspace_id,
            Approval.run_id == run_id,
            Approval.kind == ApprovalKind.BUDGET.value,
            Approval.status.in_((ApprovalStatus.APPROVED.value, ApprovalStatus.REJECTED.value)),
            Approval.idempotency_key.startswith(key_prefix),
        )
    )
    return int(await session.scalar(statement) or 0)


async def run_status_counts(session: AsyncSession, workspace_id: str) -> dict[str, int]:
    """Return ``{status: count}`` for one workspace's runs (doc 18b).

    Statuses with no rows are absent rather than zero; the route computes the
    success-rate denominator from the statuses that exist.
    """
    statement = (
        select(Run.status, func.count())
        .where(Run.workspace_id == workspace_id)
        .group_by(Run.status)
    )
    rows = (await session.execute(statement)).all()
    return {str(status): int(count) for status, count in rows}


async def finished_run_average_cost_usd(session: AsyncSession, workspace_id: str) -> Decimal | None:
    """Return the average ``run.cost_usd`` over finished runs, or ``None``.

    Finished means ``completed``, ``failed`` or ``cancelled``. SQLite returns
    ``AVG`` over ``NUMERIC`` as a float, so the value goes through ``str``
    before it becomes a ``Decimal``.
    """
    finished = (
        RunStatus.COMPLETED.value,
        RunStatus.FAILED.value,
        RunStatus.CANCELLED.value,
    )
    statement = select(func.avg(Run.cost_usd)).where(
        Run.workspace_id == workspace_id,
        Run.status.in_(finished),
    )
    average = await session.scalar(statement)
    if average is None:
        return None
    return Decimal(str(average))


async def llm_usage_cost_by_model(
    session: AsyncSession, workspace_id: str, *, start: datetime, end: datetime
) -> list[tuple[str, str, Decimal]]:
    """Return ``(provider, model, cost_usd)`` for the half-open window.

    Rows are sorted by provider then model for a deterministic dashboard.
    """
    statement = (
        select(
            LlmUsage.provider,
            LlmUsage.model,
            func.coalesce(func.sum(LlmUsage.cost_usd), 0),
        )
        .where(
            LlmUsage.workspace_id == workspace_id,
            LlmUsage.created_at >= start,
            LlmUsage.created_at < end,
        )
        .group_by(LlmUsage.provider, LlmUsage.model)
        .order_by(LlmUsage.provider, LlmUsage.model)
    )
    rows = (await session.execute(statement)).all()
    return [(str(provider), str(model), Decimal(str(cost or 0))) for provider, model, cost in rows]


async def llm_usage_cost_by_day(
    session: AsyncSession, workspace_id: str, *, start: datetime, end: datetime
) -> list[tuple[date, Decimal]]:
    """Return ``(day, cost_usd)`` for the half-open window, in UTC days.

    ``func.date`` groups by the date part of the stored UTC timestamp, which is
    exactly the "UTC days" doc 18b asks for. SQLite returns the group as a
    string while Postgres returns a date; both are normalised to ``date`` here.
    """
    day_column = func.date(LlmUsage.created_at)
    statement = (
        select(day_column, func.coalesce(func.sum(LlmUsage.cost_usd), 0))
        .where(
            LlmUsage.workspace_id == workspace_id,
            LlmUsage.created_at >= start,
            LlmUsage.created_at < end,
        )
        .group_by(day_column)
        .order_by(day_column)
    )
    rows = (await session.execute(statement)).all()
    result: list[tuple[date, Decimal]] = []
    for day_value, cost in rows:
        day = date.fromisoformat(str(day_value)) if isinstance(day_value, str) else day_value
        result.append((day, Decimal(str(cost or 0))))
    return result


def _percentile(sorted_values: list[float], percentile: int) -> float:
    """Return the nearest-rank percentile over a non-empty sorted list."""
    if not sorted_values:
        raise ValueError("cannot take a percentile of an empty list")
    index = math.ceil(percentile / 100 * len(sorted_values)) - 1
    return sorted_values[index]


async def approval_wait_stats(
    session: AsyncSession, workspace_id: str, *, start: datetime, end: datetime
) -> ApprovalWaitStats | None:
    """Return wait-time statistics over decided approvals created in the window.

    The duration is ``decided_at - created_at``. Percentiles are computed in
    Python from the fetched durations (see :class:`ApprovalWaitStats`); the SQL
    layer only fetches the two timestamps.
    """
    statement = (
        select(Approval.created_at, Approval.decided_at)
        .where(
            Approval.workspace_id == workspace_id,
            Approval.created_at >= start,
            Approval.created_at < end,
            Approval.decided_at.is_not(None),
        )
        .order_by(Approval.created_at, Approval.id)
    )
    rows = (await session.execute(statement)).all()
    durations: list[float] = []
    for created_at, decided_at in rows:
        durations.append((cast(datetime, decided_at) - created_at).total_seconds())
    if not durations:
        return None
    sorted_durations = sorted(durations)
    count = len(sorted_durations)
    return ApprovalWaitStats(
        count=count,
        avg_s=sum(sorted_durations) / count,
        p50_s=_percentile(sorted_durations, 50),
        p90_s=_percentile(sorted_durations, 90),
    )


class Repo[ModelT: Base]:
    """Query building shared by the two public repositories.

    Not used directly: it has no public read or write method, so a service
    cannot get hold of an unscoped statement through it.
    """

    #: Column ``list`` orders by. ``id`` is UUIDv7, so it is creation order.
    order_by = "id"

    def __init__(self, session: AsyncSession, model: type[ModelT], workspace_id: str) -> None:
        self.session = session
        self.model = model
        self.workspace_id = workspace_id

    @property
    def _table(self) -> Any:
        """The model, as the place to reach columns from (mypy-friendly)."""
        return self.model

    def _select(self, **filters: Any) -> Select[Any]:
        """A statement filtered by this repo's workspace and the given columns."""
        statement = select(self.model).where(self._table.workspace_id == self.workspace_id)
        conditions = [getattr(self._table, name) == value for name, value in filters.items()]
        if conditions:
            statement = statement.where(*conditions)
        return statement.order_by(getattr(self._table, self.order_by))

    def _check_workspace(self, values: dict[str, Any]) -> None:
        """Refuse a ``workspace_id`` in ``values`` that is not the repo's own."""
        given = values.pop("workspace_id", self.workspace_id)
        if given != self.workspace_id:
            raise WorkspaceMismatch(self.model.__name__, self.workspace_id, given)

    async def _insert(self, **values: Any) -> ModelT:
        """Build, add and flush a row; validation happens in the caller."""
        self._check_workspace(values)
        values["workspace_id"] = self.workspace_id
        row = self.model(**values)
        self.session.add(row)
        await self.session.flush()
        return row


class ScopedRepo[ModelT: Base](Repo[ModelT]):
    """``get``, ``list``, ``create`` and ``update`` inside one workspace."""

    async def get(self, row_id: str) -> ModelT | None:
        """Return the row with ``row_id``, or ``None`` if it is not ours.

        A row that exists in another workspace is reported exactly like a row
        that does not exist at all.
        """
        row = await self.session.get(self.model, row_id)
        if row is None or _workspace_of(row) != self.workspace_id:
            return None
        return row

    async def list(
        self,
        *,
        limit: int | None = None,
        offset: int = 0,
        after_id: str | None = None,
        **filters: Any,
    ) -> list[ModelT]:
        """Rows in this workspace, optionally narrowed by equality on columns.

        ``after_id`` is the UUIDv7 cursor: rows with an id greater than the
        cursor, which is creation order. Like every other condition, it is an
        SQLAlchemy expression compiled with a bound value, never a string built
        into SQL.
        """
        statement = self._select(**filters)
        if after_id is not None:
            statement = statement.where(self._table.id > after_id)
        statement = statement.offset(offset)
        if limit is not None:
            statement = statement.limit(limit)
        return list((await self.session.scalars(statement)).all())

    async def create(self, **values: Any) -> ModelT:
        """Insert a row in this workspace and return it, defaults applied."""
        return await self._insert(**values)

    async def update(self, row_id: str, **values: Any) -> ModelT:
        """Set columns on an existing row of this workspace."""
        row = await self.get(row_id)
        if row is None:
            raise NotFound(self.model.__name__, row_id)
        self._check_workspace(values)
        for name, value in values.items():
            setattr(row, name, value)
        await self.session.flush()
        return row

    async def delete(self, row_id: str) -> ModelT:
        """Delete an existing row of this workspace and return it."""
        row = await self.get(row_id)
        if row is None:
            raise NotFound(self.model.__name__, row_id)
        await self.session.delete(row)
        await self.session.flush()
        return row


class AppendOnlyRepo[ModelT: Base](Repo[ModelT]):
    """``append`` and ``list`` for ``event`` and ``audit_log`` (doc 03 task 6)."""

    async def append(self, **values: Any) -> ModelT:
        """Append a row. There is no update and no delete, on purpose."""
        return await self._insert(**values)

    async def list(self, *, limit: int | None = None, **filters: Any) -> list[ModelT]:
        """Rows in this workspace, oldest first."""
        statement = self._select(**filters)
        if limit is not None:
            statement = statement.limit(limit)
        return list((await self.session.scalars(statement)).all())


class EventRepo(AppendOnlyRepo[Event]):
    """The event log, ordered by ``seq`` so a client can replay from a cursor.

    ``seq`` is what SSE's ``Last-Event-ID`` carries (doc 13), which is why it is
    the order rather than ``id`` or ``ts``.
    """

    order_by = "seq"

    async def list_after(
        self,
        *,
        after_seq: int = 0,
        limit: int | None = None,
        run_id: str | None = None,
    ) -> list[Event]:
        """Events in this workspace with ``seq`` above ``after_seq``, oldest first.

        ``run_id`` optionally narrows the replay to one run. Both the SSE
        reconnect path (no run filter) and the per-run history endpoint use this
        cursor, so the ordering and the ``>`` comparison live in exactly one
        method.
        """
        statement = self._select() if run_id is None else self._select(run_id=run_id)
        statement = statement.where(self._table.seq > after_seq)
        if limit is not None:
            statement = statement.limit(limit)
        return list((await self.session.scalars(statement)).all())

    async def list_for_run(
        self, run_id: str, *, after_seq: int = 0, limit: int | None = None
    ) -> list[Event]:
        """Events of one run with ``seq`` above ``after_seq``, oldest first."""
        return await self.list_after(after_seq=after_seq, limit=limit, run_id=run_id)


class TicketRepo(ScopedRepo[Ticket]):
    """Ticket queries: filters plus the cursor-paginated ``search`` for doc 06."""

    async def search(
        self,
        *,
        status: str | None = None,
        repo_id: str | None = None,
        q: str | None = None,
        after_id: str | None = None,
        limit: int | None = None,
    ) -> list[Ticket]:
        """Tickets matching the dashboard filters, ordered by id.

        ``q`` matches ``title`` case-insensitively. ``%`` and ``_`` in ``q`` are
        escaped before the pattern is passed as a bound parameter, so a search
        term cannot change the shape of the ``LIKE`` expression.
        """
        statement = self._select()
        if status is not None:
            statement = statement.where(self._table.status == status)
        if repo_id is not None:
            statement = statement.where(self._table.repo_id == repo_id)
        if q:
            escaped = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            statement = statement.where(self._table.title.ilike(f"%{escaped}%", escape="\\"))
        if after_id is not None:
            statement = statement.where(self._table.id > after_id)
        if limit is not None:
            statement = statement.limit(limit)
        return list((await self.session.scalars(statement)).all())


class ApprovalRepo(ScopedRepo[Approval]):
    """Approval inbox queries (doc 16).

    The inbox is ordered by ``(created_at, id)`` rather than ``id`` alone:
    UUIDv7 sorts by time, but approvals created in the same millisecond would
    otherwise be ordered by their random tail, which is not "oldest first".
    """

    async def list_inbox(
        self,
        *,
        status: str | None = None,
        run_id: str | None = None,
        decided: bool = False,
        newest_first: bool = False,
        limit: int | None = None,
    ) -> list[Approval]:
        """Return approvals, optionally filtered by status/run or to decided ones.

        Oldest first by default (the inbox works through the queue in order);
        ``newest_first`` serves a "recent decisions" list.
        """
        statement = self._select().order_by(None)
        if status is not None:
            statement = statement.where(self._table.status == status)
        if decided:
            statement = statement.where(self._table.status != ApprovalStatus.PENDING.value)
        if run_id is not None:
            statement = statement.where(self._table.run_id == run_id)
        if newest_first:
            statement = statement.order_by(self._table.created_at.desc(), self._table.id.desc())
        else:
            statement = statement.order_by(self._table.created_at, self._table.id)
        if limit is not None:
            statement = statement.limit(limit)
        return list((await self.session.scalars(statement)).all())

    async def count_pending(self) -> int:
        """Return the workspace's pending approval count, ignoring page filters."""
        statement = (
            select(func.count())
            .select_from(self._table)
            .where(
                self._table.workspace_id == self.workspace_id,
                self._table.status == ApprovalStatus.PENDING.value,
            )
        )
        total = await self.session.scalar(statement)
        return int(total or 0)


class AuditLogRepo(AppendOnlyRepo[AuditLog]):
    """The audit trail, oldest first."""

    order_by = "ts"
