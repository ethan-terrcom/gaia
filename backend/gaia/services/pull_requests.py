"""Open, merge (and later watch) the GitHub PR for a run (doc 15a).

The service implements the ``open_pr`` and ``merge`` halves of the orchestrator's
:class:`gaia.orchestrator.deps.PullRequests` protocol. ``watch_ci`` belongs to
15b and is deliberately a stub.

Both methods are idempotent because a resumed LangGraph node re-runs from the top
(ARCHITECTURE section 4): opening finds the existing PR before creating one, and
merging checks GitHub's current state before calling the merge API.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from decimal import Decimal

import structlog
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from gaia.config.secrets import SecretName, SecretStore
from gaia.db.engine import session_scope
from gaia.db.models import Approval, PullRequest, Repository, RunStep, Ticket, Worktree
from gaia.db.repo import RunRepo, ScopedRepo
from gaia.domain.enums import ApprovalKind, ApprovalStatus, CiStatus, EventType, PullRequestState
from gaia.services.events import EventBus
from gaia.services.git import GitService, WorktreeLocation
from gaia.services.github import (
    GitHubClient,
    GitHubError,
    GitHubSetupError,
    RepoSlug,
    UnsupportedRemote,
    check_token,
    parse_github_remote,
)
from gaia.services.worktrees import WorktreeService

logger = structlog.get_logger(__name__)


@dataclass(frozen=True)
class _RunContext:
    """What ``open_pr`` and ``merge`` need, loaded once and then used outside the session."""

    workspace_id: str
    repo_id: str
    slug: RepoSlug
    base_branch: str
    branch: str
    location: WorktreeLocation
    ticket_title: str
    external_ref: str | None
    cost_usd: Decimal


#: The format doc 15c will write into ``ticket.external_ref``.
_EXTERNAL_REF = re.compile(r"^github#(\d+)$")

#: A PR body must stay well under GitHub's size limits; 60,000 characters leaves
#: room for the rest of the API call and is already far more than a UI can read.
MAX_PR_BODY_CHARS = 60_000

#: A zero-width joiner, inserted after every ``@`` so an agent-written summary
#: cannot ping a GitHub user.
_MENTION_ZWJ = "\u200d"

#: Anywhere an untrusted string could mention someone, neutralise the mention.
_MENTION = re.compile(r"@")


def _neutralise_mentions(text: str) -> str:
    """Return ``text`` with an @-mention made inert for GitHub notifications."""
    return _MENTION.sub(f"@{_MENTION_ZWJ}", text)


def build_pr_body(
    *,
    plan_summary: str | None,
    steps: Sequence[RunStep],
    cost_usd: Decimal,
    external_ref: str | None,
) -> str:
    """Build the plain Markdown body for a Gaia draft PR.

    ``plan_summary`` comes from the most recent approved ``plan`` approval's
    ``payload_json["plan"]["summary"]``. ``steps`` are the ``run_step`` rows for
    the run, and ``cost_usd`` is ``run.cost_usd`` rendered with two decimals.

    ``external_ref`` matching ``github#<n>`` adds ``Closes #<n>``. That is the
    format doc 15c will write when it imports an issue as a ticket.

    Ticket titles and step text are untrusted; they are rendered as plain
    Markdown and every ``@`` is followed by a zero-width joiner so a hostile
    summary cannot ping a GitHub user. The finished body is capped at
    :data:`MAX_PR_BODY_CHARS` characters.
    """
    lines: list[str] = [
        "## Plan",
        "",
        _neutralise_mentions(plan_summary) if plan_summary else "_No plan summary recorded._",
        "",
        "## Steps",
        "",
    ]
    if not steps:
        lines.append("_No steps recorded._")
    for index, step in enumerate(steps, start=1):
        lines.append(
            f"{index}. **{_neutralise_mentions(step.title)}** — "
            f"status: {_neutralise_mentions(step.status)}, attempts: {step.attempts}"
        )
        result = step.result_summary
        lines.append(
            f"   - Test results: {_neutralise_mentions(result) if result else 'not recorded'}"
        )
    lines += [
        "",
        "## Cost",
        "",
        f"${cost_usd:.2f}",
        "",
    ]
    if external_ref is not None:
        match = _EXTERNAL_REF.fullmatch(external_ref)
        if match is not None:
            lines.append(f"Closes #{match.group(1)}")
    return "\n".join(lines)[:MAX_PR_BODY_CHARS]


class PullRequestService:
    """The doc 15a implementation of the orchestrator's ``PullRequests`` protocol."""

    def __init__(
        self,
        *,
        sessions: async_sessionmaker[AsyncSession],
        git: GitService,
        secrets: SecretStore,
        bus: EventBus,
        client_factory: Callable[[SecretStr], GitHubClient] = GitHubClient,
    ) -> None:
        self.sessions = sessions
        self.git = git
        self.secrets = secrets
        self.bus = bus
        self.client_factory = client_factory
        # Token checks are cached once per process per repository (doc 15a
        # deviation: startup is not blocked, so the first open_pr is the gate).
        self._passed_token_checks: set[str] = set()
        self._token_checks_lock = asyncio.Lock()

    # ------------------------------------------------------------------ open --
    async def open_pr(self, run_id: str) -> int:
        """Open the run's draft PR, or reuse the one a resumed node already made.

        Three phases, so no network call runs inside a database transaction
        (lead review P1): load the rows, then push and call GitHub, then record
        the PR in a short transaction of its own.

        Raises :class:`GitHubSetupError` before the first push when the token is
        missing or lacks a required permission, so the node fails with that
        message instead of leaving a partial push.
        """
        async with session_scope(self.sessions) as session:
            ctx = await self._load(session, run_id)
            steps = sorted(
                await ScopedRepo(session, RunStep, ctx.workspace_id).list(run_id=run_id),
                key=lambda step: step.step_index,
            )
            plan_summary = await _plan_summary(session, ctx.workspace_id, run_id)
            title = f"[gaia] {ctx.ticket_title}"[:250]
            body = build_pr_body(
                plan_summary=plan_summary,
                steps=steps,
                cost_usd=ctx.cost_usd,
                external_ref=ctx.external_ref,
            )

        token = self.secrets.require(SecretName.GITHUB_TOKEN)
        await self._ensure_token_checked(ctx.repo_id, token, ctx.slug)
        await self.git.push(
            ctx.location, url=ctx.slug.https_url, branch=ctx.branch, token=token, timeout_s=120
        )
        async with self.client_factory(token) as client:
            existing = await client.find_open_pr(ctx.slug, ctx.branch)
            info = existing or await client.create_pr(
                ctx.slug, head=ctx.branch, base=ctx.base_branch, title=title, body=body, draft=True
            )

        async with session_scope(self.sessions) as session:
            pull_requests = ScopedRepo(session, PullRequest, ctx.workspace_id)
            rows = await pull_requests.list(run_id=run_id, number=info.number)
            newly_created = not rows
            if rows:
                await pull_requests.update(
                    rows[0].id, url=info.url, state=info.state, ci_status=CiStatus.PENDING.value
                )
            else:
                await pull_requests.create(
                    run_id=run_id,
                    number=info.number,
                    url=info.url,
                    state=info.state,
                    ci_status=CiStatus.PENDING.value,
                )

        if newly_created:
            await self.bus.publish(
                workspace_id=ctx.workspace_id,
                type=EventType.PR_OPENED,
                payload={"run_id": run_id, "number": info.number, "url": info.url},
                run_id=run_id,
            )
        return info.number

    async def _ensure_token_checked(self, repo_id: str, token: SecretStr, slug: RepoSlug) -> None:
        """Run the per-repo token check, raising on a missing permission.

        Only a passing check is cached (lead review P3): once the owner fixes the
        token's permissions, the next attempt checks again instead of failing
        until the server restarts.
        """
        async with self._token_checks_lock:
            if repo_id in self._passed_token_checks:
                return
            async with self.client_factory(token) as client:
                check = await check_token(client, slug)
            if check.ok:
                self._passed_token_checks.add(repo_id)
                return
        missing = ", ".join(check.missing)
        raise GitHubSetupError(
            f"GitHub token is missing permissions for {slug.full_name}: "
            f"{missing}. See docs/implementation/15-github-integration.md "
            "for the required fine-grained PAT scopes."
        )

    # ----------------------------------------------------------------- merge --
    async def merge(self, run_id: str, pr: int) -> None:
        """Merge the approved PR, refusing to merge a branch whose HEAD moved.

        ``sha=`` is the worktree's current HEAD: the commit Gaia pushed and the
        human approved. If anyone pushed another commit since, GitHub returns 409
        and :class:`gaia.services.github.MergeBlocked` propagates. No retry, and
        no force option exists.

        Idempotent across a resumed node (lead review P2): the merged state is
        committed as soon as GitHub reports it, and the branch delete and the
        ``pr.merged`` event run on every pass that finds the PR merged but the
        row not yet finished, including the pass after a crash.
        """
        async with session_scope(self.sessions) as session:
            ctx = await self._load(session, run_id)
            rows = await ScopedRepo(session, PullRequest, ctx.workspace_id).list(
                run_id=run_id, number=pr
            )
            if not rows:
                raise GitHubSetupError(f"run {run_id!r} has no pull request #{pr}")
            row_id = rows[0].id
            already_recorded = rows[0].state == PullRequestState.MERGED.value
        if already_recorded:
            return

        token = self.secrets.require(SecretName.GITHUB_TOKEN)
        async with self.client_factory(token) as client:
            info = await client.get_pr(ctx.slug, pr)
            if info.state != PullRequestState.MERGED.value:
                head_sha = await self.git.current_sha(ctx.location)
                # Gaia opens drafts, and GitHub will not merge a draft (G2).
                await client.mark_ready_for_review(info)
                await client.merge_pr(
                    ctx.slug,
                    pr,
                    sha=head_sha,
                    commit_title=f"[gaia] {ctx.ticket_title} (#{pr})"[:250],
                )

            async with session_scope(self.sessions) as session:
                await ScopedRepo(session, PullRequest, ctx.workspace_id).update(
                    row_id, state=PullRequestState.MERGED.value
                )

            # The merge is done and recorded; a failed branch delete is logged,
            # never allowed to undo or hide it.
            try:
                await client.delete_branch(ctx.slug, ctx.branch)
            except GitHubError:
                logger.warning("github.delete_branch_failed", run_id=run_id, pr=pr, exc_info=True)

        await self.bus.publish(
            workspace_id=ctx.workspace_id,
            type=EventType.PR_MERGED,
            payload={"run_id": run_id, "number": pr},
            run_id=run_id,
        )
        # TODO(gaia-18c): mark the worktree for cleanup.

    # ---------------------------------------------------------------- loading --
    async def _load(self, session: AsyncSession, run_id: str) -> _RunContext:
        """Load and check everything both methods need, inside one session."""
        run = await RunRepo(session).get_by_id(run_id)
        if run is None:
            raise GitHubSetupError(f"no run {run_id!r}")
        workspace_id = run.workspace_id
        ticket = await ScopedRepo(session, Ticket, workspace_id).get(run.ticket_id)
        if ticket is None:
            raise GitHubSetupError(f"run {run_id!r} has no ticket {run.ticket_id!r}")
        repository = await ScopedRepo(session, Repository, workspace_id).get(run.repo_id)
        if repository is None:
            raise GitHubSetupError(f"run {run_id!r} has no repository {run.repo_id!r}")
        if run.worktree_id is None:
            raise GitHubSetupError(f"run {run_id!r} has no worktree")
        worktree = await ScopedRepo(session, Worktree, workspace_id).get(run.worktree_id)
        if worktree is None:
            raise GitHubSetupError(f"run {run_id!r} has no worktree {run.worktree_id!r}")
        slug = _parse_repo_slug(repository)
        location = await WorktreeService(
            git=self.git, session=session, workspace_id=workspace_id
        ).location(worktree)
        return _RunContext(
            workspace_id=workspace_id,
            repo_id=repository.id,
            slug=slug,
            base_branch=repository.default_branch,
            branch=worktree.branch,
            location=location,
            ticket_title=ticket.title,
            external_ref=ticket.external_ref,
            cost_usd=run.cost_usd,
        )

    # ----------------------------------------------------------------- watch --
    async def watch_ci(self, run_id: str, pr: int) -> str:
        """Poll combined status and check runs (owned by doc 15b)."""
        raise NotImplementedError("TODO(gaia-15b)")


def _parse_repo_slug(repository: Repository) -> RepoSlug:
    """Parse ``repository.remote_url``; fail closed with the doc's message."""
    if not repository.remote_url:
        raise GitHubSetupError("this repository has no GitHub remote")
    try:
        return parse_github_remote(repository.remote_url)
    except UnsupportedRemote as exc:
        raise GitHubSetupError("this repository has no GitHub remote") from exc


async def _plan_summary(session: AsyncSession, workspace_id: str, run_id: str) -> str | None:
    """The most recent approved plan's summary, if one exists."""
    approvals = await ScopedRepo(session, Approval, workspace_id).list(run_id=run_id)
    approved = [
        approval
        for approval in approvals
        if approval.kind == ApprovalKind.PLAN.value
        and approval.status == ApprovalStatus.APPROVED.value
    ]
    if not approved:
        return None
    latest = max(
        approved,
        key=lambda approval: approval.decided_at or approval.created_at,
    )
    plan = latest.payload_json.get("plan")
    if not isinstance(plan, dict):
        return None
    summary = plan.get("summary")
    return summary if isinstance(summary, str) else None
