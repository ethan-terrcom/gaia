"""Interfaces the 12a graph nodes call; tests satisfy them with fakes.

The protocols keep the skeleton independent of the real worktree, planner,
runtime, git and GitHub services that 12b/15 wire in. Each protocol lists only
the methods a node actually calls, so a fake can be a small scriptable object.

``GraphDeps`` is the one object passed into :func:`gaia.orchestrator.graph.build_graph`.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from gaia.config.settings import Settings
from gaia.domain.plans import Plan, Review
from gaia.services.events import EventBus


@dataclass(frozen=True)
class StepOutcome:
    """What the junior runtime produced for one attempt at one step."""

    status: str
    summary: str
    files_changed: list[str]
    diff: str


@dataclass(frozen=True)
class CheckOutcome:
    """What the step's ``test_command`` produced."""

    passed: bool
    output: str


@dataclass(frozen=True)
class PlanCandidate:
    """One panelist's plan in wisdom-of-crowds mode (doc 19b)."""

    label: str
    model: str
    plan: Plan

    def as_payload(self) -> dict[str, Any]:
        """The JSON shape stored on the plan approval and shown in the UI."""
        return {"label": self.label, "model": self.model, "plan": self.plan.model_dump()}


@dataclass(frozen=True)
class PlanOutcome:
    """What planning produced: the plan, plus how the plan gate should treat it.

    ``skip_gate`` is straight shot's (doc 19b, M1): there is no plan to approve,
    so starting the run was the go-ahead. ``candidates`` and ``provenance`` are
    the crowd's (M4): the panel's plans and what the lead took from each.
    """

    plan: Plan
    skip_gate: bool = False
    candidates: tuple[PlanCandidate, ...] = ()
    provenance: tuple[str, ...] = ()
    #: Things the owner should know about how the plan was made, such as a
    #: panelist that failed.
    notes: tuple[str, ...] = ()

    def meta(self) -> dict[str, Any]:
        """The JSON-safe extras ``lead_plan`` keeps in the graph state."""
        return {
            "skip_gate": self.skip_gate,
            "candidates": [candidate.as_payload() for candidate in self.candidates],
            "provenance": list(self.provenance),
            "notes": list(self.notes),
        }


class WorktreePreparer(Protocol):
    """Create or reuse the run's worktree (doc 05/12b)."""

    async def prepare(self, run_id: str) -> str:
        """Return the worktree id for ``run_id``."""
        ...


class Planner(Protocol):
    """Produce the ticket plan (doc 11), in the run's agent mode (doc 19b)."""

    async def plan(self, run_id: str) -> PlanOutcome:
        """Return the plan to persist as ``run_step`` rows, and its gate extras."""
        ...


class StepRunner(Protocol):
    """Run one junior implementation attempt (doc 10)."""

    async def implement(
        self,
        run_id: str,
        step_index: int,
        attempt: int,
        feedback: str | None,
    ) -> StepOutcome:
        """Implement ``steps[step_index]`` and report the outcome."""
        ...


class CheckRunner(Protocol):
    """Run a step's ``test_command`` (doc 12b)."""

    async def run(self, run_id: str, step_index: int) -> CheckOutcome:
        """Return whether the step's checks passed."""
        ...


class Reviewer(Protocol):
    """The lead's review of a step and of the final diff (doc 11)."""

    async def review_step(
        self,
        run_id: str,
        step_index: int,
        outcome: StepOutcome,
        checks: CheckOutcome,
    ) -> Review:
        """Review one implementation attempt."""
        ...

    async def final_review(self, run_id: str) -> Review:
        """Review the complete diff before opening a PR."""
        ...


class Committer(Protocol):
    """Commit an accepted step (doc 05/12b)."""

    async def commit_step(self, run_id: str, step_index: int, title: str) -> str:
        """Commit the step and return the commit SHA."""
        ...


class PullRequests(Protocol):
    """Open, watch and merge the PR for a run (doc 15/12b)."""

    async def open_pr(self, run_id: str) -> int:
        """Open the PR and return its number."""
        ...

    async def watch_ci(self, run_id: str, pr: int) -> str:
        """Return ``"passed"`` or ``"failed"`` for the PR's CI rollup."""
        ...

    async def merge(self, run_id: str, pr: int) -> None:
        """Merge the approved PR."""
        ...


@dataclass(frozen=True)
class GraphDeps:
    """Everything the graph nodes and :class:`~gaia.orchestrator.runner.RunManager` need."""

    worktrees: WorktreePreparer
    planner: Planner
    steps: StepRunner
    checks: CheckRunner
    reviewer: Reviewer
    committer: Committer
    pull_requests: PullRequests
    sessions: async_sessionmaker[AsyncSession]
    events: EventBus
    settings: Settings
    #: Clock for budget checks. ``None`` uses the guard's default; tests can
    #: inject a frozen clock through ``make_graph_deps``.
    now: Callable[[], datetime] | None = None
