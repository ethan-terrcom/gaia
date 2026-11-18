"""The real reviewer: lead review calls through the gateway (12b-2), per agent mode (19b).

The run's lead model reviews in every mode. In straight shot that is the one
model reviewing its own work (owner decision M1), and the final review is
skipped: the plan has one step, so its review already covered the whole diff.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from gaia.config.settings import Settings
from gaia.db.engine import session_scope
from gaia.db.models import Approval, Repository, Run, RunStep, Worktree
from gaia.db.repo import RunRepo, ScopedRepo
from gaia.domain.agent_modes import AgentMode, AgentModeConfig
from gaia.domain.enums import ApprovalKind
from gaia.domain.plans import Plan, PlanStep, Review
from gaia.orchestrator.deps import CheckOutcome, StepOutcome
from gaia.orchestrator.impl.lead_llm import lead_llm
from gaia.orchestrator.lead import LeadEngineer
from gaia.security.commands import CommandPolicy
from gaia.services.agent_modes import run_agent_config
from gaia.services.costs import PriceTable
from gaia.services.git import GitService, WorktreeLocation


def plan_from_rows(
    steps: Sequence[RunStep],
    approval_payload: Mapping[str, Any] | None,
) -> Plan:
    """Rebuild a :class:`Plan` from ``run_step`` rows and a plan approval.

    The steps reflect the edited plan that was actually approved, so they come
    from ``run_step`` rows ordered by ``step_index``. ``summary`` and ``risks``
    are not stored on the steps; they come from the plan approval's
    ``payload_json["plan"]``, falling back to empty values when no approval
    exists yet.
    """
    if approval_payload is None:
        plan_payload: Mapping[str, Any] = {}
    else:
        raw_plan = approval_payload.get("plan")
        plan_payload = raw_plan if isinstance(raw_plan, Mapping) else {}

    summary_raw = plan_payload.get("summary", "")
    summary = summary_raw if isinstance(summary_raw, str) else ""
    risks_raw = plan_payload.get("risks", [])
    risks = list(risks_raw) if isinstance(risks_raw, list) else []

    return Plan(
        summary=summary,
        risks=risks,
        steps=[PlanStep.model_validate(step.spec_json) for step in steps],
    )


#: The final review straight shot records instead of a second model call.
STRAIGHT_SHOT_FINAL_REVIEW = Review(
    verdict="accept",
    feedback="Straight shot: the step review already covered the whole diff.",
    issues=[],
)


@dataclass(frozen=True)
class _FinalReviewContext:
    plan: Plan
    workspace_id: str
    worktree: Worktree
    repository: Repository
    config: AgentModeConfig


class GatewayReviewer:
    """The lead's step and final reviews, both through the gateway."""

    def __init__(
        self,
        *,
        settings: Settings,
        sessions: async_sessionmaker[AsyncSession],
        price_table: PriceTable,
        git: GitService,
        http_client: Any | None = None,
    ) -> None:
        self._settings = settings
        self._sessions = sessions
        self._price_table = price_table
        self._git = git
        self._http_client = http_client

    async def review_step(
        self,
        run_id: str,
        step_index: int,
        outcome: StepOutcome,
        checks: CheckOutcome,
    ) -> Review:
        """Review one implementation attempt with the lead's structured LLM."""
        plan, workspace_id, config = await self._load_plan(run_id)
        async with lead_llm(
            settings=self._settings,
            sessions=self._sessions,
            price_table=self._price_table,
            run_id=run_id,
            workspace_id=workspace_id,
            model=config.lead_model,
            http_client=self._http_client,
        ) as llm:
            engineer = LeadEngineer(llm, CommandPolicy.from_default())
            return await engineer.review_step(plan, step_index, outcome.diff, checks.output)

    async def final_review(self, run_id: str) -> Review:
        """Review the full branch diff with the lead's structured LLM."""
        context = await self._load_final_review_context(run_id)
        if context.config.mode is AgentMode.SINGLE and len(context.plan.steps) == 1:
            return STRAIGHT_SHOT_FINAL_REVIEW
        if context.worktree.base_sha is None:
            raise ValueError(f"worktree {context.worktree.id!r} has no base_sha")
        location = await self._worktree_location(context.worktree, context.repository)
        diff = await self._git.diff(location, context.worktree.base_sha, "HEAD")

        async with lead_llm(
            settings=self._settings,
            sessions=self._sessions,
            price_table=self._price_table,
            run_id=run_id,
            workspace_id=context.workspace_id,
            model=context.config.lead_model,
            http_client=self._http_client,
        ) as llm:
            engineer = LeadEngineer(llm, CommandPolicy.from_default())
            return await engineer.final_review(context.plan, diff)

    def _config(self, run: Run) -> AgentModeConfig:
        """The agent config ``run`` executes with."""
        return run_agent_config(
            run.agent_config, settings=self._settings, junior_runtime=run.junior_runtime
        )

    async def _load_plan(self, run_id: str) -> tuple[Plan, str, AgentModeConfig]:
        """Load a run's approved/edited plan, workspace and config in one short session."""
        async with session_scope(self._sessions) as session:
            run = await RunRepo(session).get_by_id(run_id)
            if run is None:
                raise ValueError(f"no run with id {run_id!r}")
            plan = await self._plan_for_run(session, run.workspace_id, run_id)
            return plan, run.workspace_id, self._config(run)

    async def _load_final_review_context(self, run_id: str) -> _FinalReviewContext:
        """Load the plan and worktree needed for a final review."""
        async with session_scope(self._sessions) as session:
            run = await RunRepo(session).get_by_id(run_id)
            if run is None:
                raise ValueError(f"no run with id {run_id!r}")
            if run.worktree_id is None:
                raise ValueError(f"run {run_id!r} has no worktree")
            worktree = await ScopedRepo(session, Worktree, run.workspace_id).get(run.worktree_id)
            if worktree is None:
                raise ValueError(f"run {run_id!r} has no worktree {run.worktree_id!r}")
            repository = await ScopedRepo(session, Repository, run.workspace_id).get(run.repo_id)
            if repository is None:
                raise ValueError(f"run {run_id!r} has no repository {run.repo_id!r}")
            plan = await self._plan_for_run(session, run.workspace_id, run_id)
            return _FinalReviewContext(
                plan=plan,
                workspace_id=run.workspace_id,
                worktree=worktree,
                repository=repository,
                config=self._config(run),
            )

    @staticmethod
    async def _plan_for_run(
        session: AsyncSession,
        workspace_id: str,
        run_id: str,
    ) -> Plan:
        """Rebuild a run's plan from its steps and most recent plan approval."""
        step_rows = await ScopedRepo(session, RunStep, workspace_id).list(run_id=run_id)
        step_rows = sorted(step_rows, key=lambda step: step.step_index)
        approval_rows = await ScopedRepo(session, Approval, workspace_id).list(
            run_id=run_id,
            kind=ApprovalKind.PLAN.value,
        )
        # UUIDv7 ids are time-ordered across milliseconds but not monotonic
        # within one, so "most recent" is ``created_at``, not list order.
        approval_rows = sorted(approval_rows, key=lambda row: row.created_at)
        approval_payload = approval_rows[-1].payload_json if approval_rows else None
        return plan_from_rows(step_rows, approval_payload)

    async def _worktree_location(
        self,
        worktree: Worktree,
        repository: Repository,
    ) -> WorktreeLocation:
        """Return the pinned worktree location, deriving ``git_dir`` if needed."""
        work_tree = Path(worktree.path)
        if worktree.git_dir is not None:
            return WorktreeLocation(work_tree=work_tree, git_dir=Path(worktree.git_dir))
        repo_path = await self._git.validate_repo(repository.local_path)
        git_dir = await self._git.worktree_admin_dir(repo_path, work_tree)
        return WorktreeLocation(work_tree=work_tree, git_dir=git_dir)
