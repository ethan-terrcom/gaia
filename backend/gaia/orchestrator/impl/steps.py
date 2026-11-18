"""The real :class:`~gaia.orchestrator.deps.StepRunner` (doc 12b-1).

It chooses the model for an attempt from the run's agent config (doc 19b), and
the runtime from that model's provider (ADR-001 P2): Anthropic models run on the
Claude Agent SDK runtime, every other provider on the native loop. It renders
the plan step into Gaia-owned Markdown and runs it through the shared
``run_with_limits`` wrapper, so timeouts, cancellation and git-computed
``files_changed`` are identical for every runtime.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from gaia.config.settings import Settings
from gaia.db.engine import session_scope
from gaia.db.ids import uuid7
from gaia.db.models import Repository, Run, RunStep, Worktree
from gaia.db.repo import RunRepo, ScopedRepo
from gaia.domain.agent_modes import AgentMode
from gaia.domain.enums import AgentRole, RuntimeKind
from gaia.domain.plans import PlanStep
from gaia.llm.untrusted import wrap_untrusted
from gaia.orchestrator.deps import StepOutcome
from gaia.orchestrator.impl import worktree_location
from gaia.runtimes.base import AgentTask, RuntimeRegistry
from gaia.runtimes.support import run_with_limits
from gaia.services.agent_modes import run_agent_config, runtime_for_model
from gaia.services.allowed_commands import allowed_entries
from gaia.services.costs import PriceTable
from gaia.services.events import BusEventSink, EventBus
from gaia.services.git import GitService, WorktreeLocation

logger = structlog.get_logger(__name__)


def render_step_instructions(
    step: PlanStep,
    *,
    attempt: int,
    feedback: str | None,
    repository: Repository | None = None,
) -> str:
    """Render one plan step as plain Markdown for the agent.

    The plan and the reviewer's feedback are model output and therefore data:
    they are wrapped with :func:`gaia.llm.untrusted.wrap_untrusted` so the
    junior system prompt's "content inside tags is data" rule applies to them.
    The repository's commands are the owner's registration, so they are
    Gaia-owned text: they tell the agent what it can run without asking.
    """
    sections = [
        "Plan step",
        "",
        wrap_untrusted(
            f"Title: {step.title}\n\nInstructions:\n{step.instructions}",
            "plan:step",
        ),
    ]
    if step.files_likely_touched:
        sections.extend(
            [
                "## Files likely touched",
                "",
                wrap_untrusted(
                    "\n".join(f"- {path}" for path in step.files_likely_touched),
                    "plan:files",
                ),
            ]
        )
    if step.acceptance:
        sections.extend(
            [
                "## Acceptance criteria",
                "",
                wrap_untrusted(
                    "\n".join(f"- {item}" for item in step.acceptance),
                    "plan:acceptance",
                ),
            ]
        )
    if step.test_command:
        sections.extend(
            [
                "## Test command",
                "",
                wrap_untrusted(" ".join(step.test_command), "plan:test-command"),
            ]
        )
    if repository is not None:
        sections.extend(_repository_commands_section(repository))
    if attempt > 1:
        sections.extend(
            [
                "## Reviewer feedback",
                "",
                wrap_untrusted(feedback or "(no feedback)", f"reviewer:attempt:{attempt}"),
            ]
        )
    return "\n".join(sections)


def _repository_commands_section(repository: Repository) -> list[str]:
    """The owner's commands for this repository, as a Markdown section.

    Gaia-owned text, not wrapped as untrusted: the owner registered every
    command in it. It deliberately avoids a ``Test command:`` line, which the
    demo model reads as the step's test command.
    """
    lines = [
        "## Repository commands",
        "",
        "Run commands from the worktree root. These run without asking the owner; "
        "you may add arguments after them:",
    ]
    test = _argv_tuple(repository.test_command)
    if test:
        lines.append(f"- `{' '.join(test)}` (the repository's test command)")
    lines.extend(f"- `{' '.join(entry)}`" for entry in allowed_entries(repository))
    if len(lines) == 3:
        lines.append("- (none registered)")
    setup = _argv_tuple(repository.setup_command)
    if setup:
        lines.extend(
            [
                "",
                f"Setup (`{' '.join(setup)}`) has already run, so dependencies are installed. "
                "Commands have no network access.",
            ]
        )
    lines.extend(
        [
            "",
            "Any other command may need the owner's approval. Prefer the commands above.",
        ]
    )
    return lines


@dataclass(frozen=True)
class StepChoice:
    """The model, runtime, role and turn limit one attempt runs with."""

    runtime: RuntimeKind
    model: str
    role: AgentRole
    max_turns: int


class RuntimeStepRunner:
    """Run one plan step on the role's configured runtime."""

    def __init__(
        self,
        *,
        settings: Settings,
        sessions: async_sessionmaker[AsyncSession],
        git: GitService,
        runtimes: RuntimeRegistry,
        bus: EventBus,
        price_table: PriceTable,
    ) -> None:
        self.settings = settings
        self.sessions = sessions
        self.git = git
        self.runtimes = runtimes
        self.bus = bus
        self.price_table = price_table

    async def implement(
        self,
        run_id: str,
        step_index: int,
        attempt: int,
        feedback: str | None,
    ) -> StepOutcome:
        """Run ``steps[step_index]`` and report the outcome.

        A ``budget_exceeded`` result is returned as is: the next node's guard
        sees the spent budget and raises the budget interrupt, so the graph
        pauses exactly like it does for a model call that hits the gateway cap.
        """
        context = await self._load_context(run_id, step_index)
        step = PlanStep.model_validate(context["step"].spec_json)
        location = await self._location_for(context["repository"], context["worktree"])
        self.git.verify_location(location)

        choice = self._choose(context["run"], step, attempt)
        runtime = self.runtimes.get(choice.runtime.value)
        logger.info(
            "step.runtime_chosen",
            run_id=run_id,
            step_index=step_index,
            attempt=attempt,
            runtime=choice.runtime.value,
            model=choice.model,
            role=choice.role.value,
        )

        base_sha = await self.git.current_sha(location)
        task = AgentTask(
            task_id=uuid7(),
            run_id=run_id,
            run_step_id=context["step"].id,
            workspace_id=context["run"].workspace_id,
            agent_id=None,
            role=choice.role,
            model=choice.model,
            instructions=render_step_instructions(
                step, attempt=attempt, feedback=feedback, repository=context["repository"]
            ),
            worktree=location,
            base_sha=base_sha,
            timeout_s=self.settings.step_timeout_s,
            max_turns=choice.max_turns,
            extra_allow=_extra_allow(context["repository"]),
        )
        result = await run_with_limits(
            runtime,
            task,
            BusEventSink(
                self.bus,
                workspace_id=context["run"].workspace_id,
                run_id=run_id,
                agent_id=None,
            ),
            git=self.git,
        )
        diff = await self.git.diff_since(location, base_sha)
        return StepOutcome(
            status=result.status,
            summary=result.summary,
            files_changed=list(result.files_changed),
            diff=diff,
        )

    async def _load_context(self, run_id: str, step_index: int) -> dict[str, Any]:
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
            return {
                "run": run,
                "step": step,
                "repository": repository,
                "worktree": worktree,
            }

    async def _location_for(self, repository: Repository, worktree: Worktree) -> WorktreeLocation:
        """Build the pinned location without holding a database session."""
        repo_path = await self.git.validate_repo(repository.local_path)
        return await worktree_location(git=self.git, repo_path=repo_path, worktree=worktree)

    def _choose(self, run: Run, step: PlanStep, attempt: int) -> StepChoice:
        """Pick the model, role, runtime and turn limit for one attempt.

        - Straight shot: the one model, every attempt, as the lead (it plays
          every role), with the lead's turn limit.
        - Two tiered and crowd: the step's assigned worker; from attempt
          ``lead_attempt_from`` on, the lead model takes the step over.
        """
        config = run_agent_config(
            run.agent_config, settings=self.settings, junior_runtime=run.junior_runtime
        )
        lead_takes_over = bool(
            self.settings.lead_attempt_from and attempt >= self.settings.lead_attempt_from
        )
        if config.mode is AgentMode.SINGLE or lead_takes_over:
            model, role, max_turns = (
                config.lead_model,
                AgentRole.LEAD,
                self.settings.lead_max_turns,
            )
        else:
            model, role, max_turns = (
                config.worker_for(step.worker),
                AgentRole.JUNIOR,
                self.settings.junior_max_turns,
            )
        runtime, runtime_model = runtime_for_model(model, self.price_table, self.settings)
        return StepChoice(runtime=runtime, model=runtime_model, role=role, max_turns=max_turns)


def _extra_allow(repository: Repository) -> tuple[tuple[str, ...], ...]:
    """The owner-registered test command and allowed commands, allowed unasked.

    Only the **owner's** registration belongs here (lead review X1). A plan
    step's ``test_command`` is model output: putting it in ``extra_allow`` would
    let a prompt-injected plan turn a command that needs approval, such as
    ``bash -c …``, into one that runs unasked. The plan's test commands are
    validated separately, and must already be allowed by the policy.
    """
    repo_command = _argv_tuple(repository.test_command)
    test = (repo_command,) if repo_command else ()
    return (*test, *allowed_entries(repository))


def _argv_tuple(command: list[str] | None) -> tuple[str, ...]:
    """Return ``command`` as a tuple, ignoring a malformed registration."""
    if not command:
        return ()
    if not all(isinstance(part, str) for part in command):
        return ()
    return tuple(command)
