"""The real planner: lead planning through the gateway (12b-2), per agent mode (19b).

- **Straight shot** makes no model call: the plan is the ticket, as one step, and
  the plan gate is skipped (owner decision M1).
- **Two tiered** is one structured call on the run's lead model, which also
  assigns each step to a worker (M2).
- **Wisdom of crowds** asks every panelist for a plan in parallel, then one call
  on the lead merges them (M4). A panelist that fails is skipped and noted; if
  none succeeds, the lead plans alone.
"""

from __future__ import annotations

import asyncio
import string
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from gaia.config.settings import Settings
from gaia.db.engine import session_scope
from gaia.db.models import Repository, Ticket, Worktree
from gaia.db.repo import RunRepo, ScopedRepo
from gaia.domain.agent_modes import AgentMode, AgentModeConfig
from gaia.domain.plans import (
    MAX_STEP_INSTRUCTIONS_CHARS,
    MAX_STEP_TITLE_CHARS,
    Plan,
    PlanStep,
    TicketInput,
)
from gaia.llm.base import LLMBudgetExceeded, StructuredLLM
from gaia.orchestrator.deps import PlanCandidate, PlanOutcome
from gaia.orchestrator.impl.lead_llm import lead_llm
from gaia.orchestrator.lead import LeadEngineer
from gaia.orchestrator.repo_context import RepoContext, build_repo_context
from gaia.security.commands import CommandPolicy
from gaia.services.agent_modes import run_agent_config
from gaia.services.costs import PriceTable

logger = structlog.get_logger(__name__)

#: Panel labels, in panel order: the lead sees "Plan A", never a model name.
PANEL_LABELS = string.ascii_uppercase


@dataclass(frozen=True)
class _PlanningContext:
    """Everything a planning call needs, loaded in one short session."""

    workspace_id: str
    config: AgentModeConfig
    ticket: TicketInput
    worktree_root: Path
    test_command: tuple[str, ...]


def straight_shot_plan(ticket: TicketInput, model: str) -> Plan:
    """The one-step plan straight shot runs: the ticket itself (M1).

    The step carries no ``test_command``, so the check node runs the
    repository's registered one. The ticket text becomes the step's
    instructions, which the step runner wraps as untrusted data.
    """
    title = (ticket.title.strip() or "Do the ticket")[:MAX_STEP_TITLE_CHARS]
    body = ticket.body.strip()
    instructions = f"Do this ticket in full.\n\nTitle: {ticket.title}\n\n{body}".strip()
    return Plan(
        summary=f"Straight shot: {model} does the whole ticket in one session.",
        risks=[],
        steps=[
            PlanStep(
                title=title,
                instructions=instructions[:MAX_STEP_INSTRUCTIONS_CHARS],
                files_likely_touched=[],
                acceptance=[
                    "Everything the ticket asks for is done.",
                    "The repository's tests pass.",
                ],
                test_command=None,
            )
        ],
    )


def assign_workers(plan: Plan, config: AgentModeConfig) -> Plan:
    """Pin every step's ``worker`` to a model in the run's pool.

    A missing or unknown worker becomes the first one, so the plan the owner
    approves shows the model each step will really run on.
    """
    steps = [
        step.model_copy(update={"worker": config.worker_for(step.worker)}) for step in plan.steps
    ]
    return plan.model_copy(update={"steps": steps})


def workers_note(config: AgentModeConfig, prices: PriceTable) -> str:
    """Gaia's description of the worker pool for the lead's planning message."""
    lines = [
        "## Workers",
        "",
        "Set each step's `worker` to one of these ids:",
    ]
    for model in config.worker_models:
        price = prices.get(model)
        if price is None:
            lines.append(f"- `{model}`")
            continue
        tier = f"{price.tier} tier, " if price.tier else ""
        lines.append(
            f"- `{model}`: {price.label or model} ({tier}${price.rates.input} in / "
            f"${price.rates.output} out per 1M tokens)"
        )
    return "\n".join(lines)


class GatewayPlanner:
    """Produce a ticket plan with the run's lead (and panel) through the gateway."""

    def __init__(
        self,
        *,
        settings: Settings,
        sessions: async_sessionmaker[AsyncSession],
        price_table: PriceTable,
        http_client: Any | None = None,
    ) -> None:
        self._settings = settings
        self._sessions = sessions
        self._price_table = price_table
        self._http_client = http_client

    async def plan(self, run_id: str) -> PlanOutcome:
        """Plan ``run_id``'s ticket in its agent mode, holding no DB session during calls."""
        context = await self._load(run_id)
        config = context.config
        if config.mode is AgentMode.SINGLE:
            return PlanOutcome(
                plan=straight_shot_plan(context.ticket, config.lead_model), skip_gate=True
            )

        repo_context = await build_repo_context(context.worktree_root, context.ticket)
        note = workers_note(config, self._price_table)
        if config.mode is AgentMode.TIERED:
            plan = await self._plan_on(run_id, context, repo_context, config.lead_model, note)
            return PlanOutcome(plan=assign_workers(plan, config))
        return await self._crowd(run_id, context, repo_context, note)

    async def _crowd(
        self,
        run_id: str,
        context: _PlanningContext,
        repo_context: RepoContext,
        note: str,
    ) -> PlanOutcome:
        """Panel plans in parallel, then the lead merges them (M4)."""
        config = context.config
        labelled = list(zip(PANEL_LABELS, config.panel_models, strict=False))
        results = await asyncio.gather(
            *(self._plan_on(run_id, context, repo_context, model, None) for _, model in labelled),
            return_exceptions=True,
        )
        candidates: list[PlanCandidate] = []
        notes: list[str] = []
        for (label, model), result in zip(labelled, results, strict=True):
            if isinstance(result, LLMBudgetExceeded):
                # The budget guard must see this: the run pauses, it does not
                # carry on with a smaller panel.
                raise result
            if isinstance(result, Exception):
                logger.warning(
                    "planning.panelist_failed",
                    run_id=run_id,
                    label=label,
                    model=model,
                    error=type(result).__name__,
                )
                notes.append(f"Plan {label} ({model}) failed: {type(result).__name__}.")
                continue
            if isinstance(result, BaseException):
                raise result
            candidates.append(PlanCandidate(label=label, model=model, plan=result))

        if not candidates:
            notes.append("No panelist produced a plan, so the lead planned alone.")
            plan = await self._plan_on(run_id, context, repo_context, config.lead_model, note)
            return PlanOutcome(plan=assign_workers(plan, config), notes=tuple(notes))

        async with self._llm(run_id, context.workspace_id, config.lead_model) as llm:
            merged = await LeadEngineer(llm, CommandPolicy.from_default()).synthesize_plan(
                context.ticket,
                repo_context,
                [(candidate.label, candidate.plan) for candidate in candidates],
                extra_allow=_extra_allow(context.test_command),
                test_command=context.test_command or None,
                workers_note=note,
            )
        return PlanOutcome(
            plan=assign_workers(merged.as_plan(), config),
            candidates=tuple(candidates),
            provenance=tuple(merged.provenance),
            notes=tuple(notes),
        )

    async def _plan_on(
        self,
        run_id: str,
        context: _PlanningContext,
        repo_context: RepoContext,
        model: str,
        note: str | None,
    ) -> Plan:
        """One planning call on ``model``, with its own short-lived run token."""
        async with self._llm(run_id, context.workspace_id, model) as llm:
            engineer = LeadEngineer(llm, CommandPolicy.from_default())
            return await engineer.plan_ticket(
                context.ticket,
                repo_context,
                extra_allow=_extra_allow(context.test_command),
                test_command=context.test_command or None,
                workers_note=note,
            )

    def _llm(
        self, run_id: str, workspace_id: str, model: str
    ) -> AbstractAsyncContextManager[StructuredLLM]:
        """A structured LLM on ``model`` for one call, through the gateway."""
        return lead_llm(
            settings=self._settings,
            sessions=self._sessions,
            price_table=self._price_table,
            run_id=run_id,
            workspace_id=workspace_id,
            model=model,
            http_client=self._http_client,
        )

    async def _load(self, run_id: str) -> _PlanningContext:
        """Load the run's rows and agent config in one short session."""
        async with session_scope(self._sessions) as session:
            run = await RunRepo(session).get_by_id(run_id)
            if run is None:
                raise ValueError(f"no run with id {run_id!r}")
            workspace_id = run.workspace_id
            ticket = await ScopedRepo(session, Ticket, workspace_id).get(run.ticket_id)
            if ticket is None:
                raise ValueError(f"run {run_id!r} has no ticket {run.ticket_id!r}")
            repository = await ScopedRepo(session, Repository, workspace_id).get(run.repo_id)
            if repository is None:
                raise ValueError(f"run {run_id!r} has no repository {run.repo_id!r}")
            if run.worktree_id is None:
                raise ValueError(f"run {run_id!r} has no worktree")
            worktree = await ScopedRepo(session, Worktree, workspace_id).get(run.worktree_id)
            if worktree is None:
                raise ValueError(f"run {run_id!r} has no worktree {run.worktree_id!r}")
            return _PlanningContext(
                workspace_id=workspace_id,
                config=run_agent_config(
                    run.agent_config,
                    settings=self._settings,
                    junior_runtime=run.junior_runtime,
                ),
                ticket=TicketInput(id=ticket.id, title=ticket.title, body=ticket.body or ""),
                worktree_root=Path(worktree.path),
                test_command=(tuple(repository.test_command) if repository.test_command else ()),
            )


def _extra_allow(test_command: tuple[str, ...]) -> tuple[tuple[str, ...], ...]:
    """The owner's registered test command, the one plan command allowed unasked."""
    return (test_command,) if test_command else ()
