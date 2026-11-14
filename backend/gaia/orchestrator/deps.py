"""Interfaces the 12a graph nodes call; tests satisfy them with fakes.

The protocols keep the skeleton independent of the real worktree, planner,
runtime, git and GitHub services that 12b/15 wire in. Each protocol lists only
the methods a node actually calls, so a fake can be a small scriptable object.

``GraphDeps`` is the one object passed into :func:`gaia.orchestrator.graph.build_graph`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

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


class WorktreePreparer(Protocol):
    """Create or reuse the run's worktree (doc 05/12b)."""

    async def prepare(self, run_id: str) -> str:
        """Return the worktree id for ``run_id``."""
        ...


class Planner(Protocol):
    """Produce the ticket plan (doc 11)."""

    async def plan(self, run_id: str) -> Plan:
        """Return the plan to persist as ``run_step`` rows."""
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
