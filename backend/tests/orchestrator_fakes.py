"""Scriptable fakes for the 12a graph interfaces.

These live in ``tests/`` on purpose: 12a is the graph skeleton, and the real
implementations arrive with 12b/15. Every fake records its calls so a test can
assert what the graph actually drove, and outcomes are simple dictionaries keyed
by step.
"""

from __future__ import annotations

import asyncio

from gaia.domain.plans import Plan, PlanStep, Review
from gaia.orchestrator.deps import CheckOutcome, StepOutcome


def make_plan(step_count: int = 2) -> Plan:
    """Return a valid plan with ``step_count`` simple steps."""
    return Plan(
        summary="A test plan",
        risks=[],
        steps=[
            PlanStep(
                title=f"Step {index + 1}",
                instructions=f"Implement step {index + 1}",
                files_likely_touched=[f"file_{index + 1}.py"],
                acceptance=[f"Step {index + 1} works"],
                test_command=["uv", "run", "pytest", "-q"],
            )
            for index in range(step_count)
        ],
    )


def make_review(
    verdict: str = "accept", feedback: str = "", issues: list[str] | None = None
) -> Review:
    """Return a review with the given verdict."""
    return Review(verdict=verdict, feedback=feedback, issues=issues or [])  # type: ignore[arg-type]


class FakeWorktreePreparer:
    """Returns a fixed worktree id."""

    def __init__(self, worktree_id: str = "worktree-1") -> None:
        self.worktree_id = worktree_id
        self.calls: list[str] = []

    async def prepare(self, run_id: str) -> str:
        self.calls.append(run_id)
        return self.worktree_id


class FakePlanner:
    """Returns a fixed plan."""

    def __init__(self, plan: Plan | None = None) -> None:
        self.plan_value = plan or make_plan()
        self.calls: list[str] = []

    async def plan(self, run_id: str) -> Plan:
        self.calls.append(run_id)
        return self.plan_value


class FakeStepRunner:
    """Returns scriptable outcomes and can block mid-implementation."""

    def __init__(
        self,
        default_outcome: StepOutcome | None = None,
        *,
        block_event: asyncio.Event | None = None,
        block_step: int | None = None,
    ) -> None:
        self.default_outcome = default_outcome or StepOutcome(
            status="completed",
            summary="implemented",
            files_changed=["file.py"],
            diff="diff --git a/file.py b/file.py\n+change\n",
        )
        self.outcomes: dict[tuple[int, int], StepOutcome] = {}
        self.calls: list[tuple[str, int, int, str | None]] = []
        self.block_event = block_event
        self.block_step = block_step
        self._calls_event = asyncio.Event()

    async def wait_for_calls(self, count: int) -> None:
        """Wait until ``implement`` has been called at least ``count`` times."""
        while len(self.calls) < count:
            self._calls_event.clear()
            await self._calls_event.wait()

    async def implement(
        self,
        run_id: str,
        step_index: int,
        attempt: int,
        feedback: str | None,
    ) -> StepOutcome:
        self.calls.append((run_id, step_index, attempt, feedback))
        self._calls_event.set()
        if self.block_event is not None and (
            self.block_step is None or self.block_step == step_index
        ):
            await self.block_event.wait()
        return self.outcomes.get((step_index, attempt), self.default_outcome)


class FakeCheckRunner:
    """Returns scriptable check outcomes."""

    def __init__(self, default: CheckOutcome | None = None) -> None:
        self.default = default or CheckOutcome(passed=True, output="all tests passed")
        self.results: dict[int, CheckOutcome] = {}
        self.calls: list[tuple[str, int]] = []

    async def run(self, run_id: str, step_index: int) -> CheckOutcome:
        self.calls.append((run_id, step_index))
        return self.results.get(step_index, self.default)


class FakeReviewer:
    """Returns scriptable step and final reviews."""

    def __init__(
        self,
        step_reviews: dict[int, Review] | None = None,
        *,
        step_review_sequence: list[Review] | None = None,
        final_review: Review | None = None,
        default_step_review: Review | None = None,
    ) -> None:
        self.step_reviews = step_reviews or {}
        self.step_review_sequence = step_review_sequence or []
        self.final_review_value = final_review or make_review()
        self.default_step_review = default_step_review or make_review()
        self.review_step_calls: list[tuple[str, int, StepOutcome, CheckOutcome]] = []
        self.final_review_calls: list[str] = []

    async def review_step(
        self,
        run_id: str,
        step_index: int,
        outcome: StepOutcome,
        checks: CheckOutcome,
    ) -> Review:
        self.review_step_calls.append((run_id, step_index, outcome, checks))
        call_index = len(self.review_step_calls) - 1
        if call_index < len(self.step_review_sequence):
            return self.step_review_sequence[call_index]
        return self.step_reviews.get(step_index, self.default_step_review)

    async def final_review(self, run_id: str) -> Review:
        self.final_review_calls.append(run_id)
        return self.final_review_value


class FakeCommitter:
    """Records commits and returns deterministic SHAs."""

    def __init__(self, sha_prefix: str = "sha") -> None:
        self.sha_prefix = sha_prefix
        self.calls: list[tuple[str, int, str]] = []

    async def commit_step(self, run_id: str, step_index: int, title: str) -> str:
        self.calls.append((run_id, step_index, title))
        return f"{self.sha_prefix}-{step_index}"


class FakePullRequests:
    """Records PR calls and returns scriptable PR/CI outcomes."""

    def __init__(
        self,
        *,
        pr_number: int = 42,
        ci_results: dict[int, str] | None = None,
        default_ci: str = "passed",
    ) -> None:
        self.pr_number = pr_number
        self.ci_results = ci_results or {}
        self.default_ci = default_ci
        self.open_calls: list[str] = []
        self.watch_calls: list[tuple[str, int]] = []
        self.merge_calls: list[tuple[str, int]] = []

    async def open_pr(self, run_id: str) -> int:
        self.open_calls.append(run_id)
        return self.pr_number

    async def watch_ci(self, run_id: str, pr: int) -> str:
        self.watch_calls.append((run_id, pr))
        return self.ci_results.get(pr, self.default_ci)

    async def merge(self, run_id: str, pr: int) -> None:
        self.merge_calls.append((run_id, pr))


def make_outcome(status: str = "completed", *, summary: str = "done") -> StepOutcome:
    """Build a step outcome with a small diff."""
    return StepOutcome(
        status=status,
        summary=summary,
        files_changed=["file.py"],
        diff="diff --git a/file.py b/file.py\n+change\n",
    )


def make_check_outcome(passed: bool = True, output: str = "tests passed") -> CheckOutcome:
    """Build a check outcome."""
    return CheckOutcome(passed=passed, output=output)


def with_step_review(review: Review, *, step_index: int = 0) -> dict[int, Review]:
    """Return a step-review map with one entry."""
    return {step_index: review}
