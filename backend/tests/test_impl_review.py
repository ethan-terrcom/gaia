"""GatewayReviewer and plan_from_rows (12b-2)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, cast

import pytest

from gaia.config.settings import Settings
from gaia.db.engine import session_scope
from gaia.db.models import Approval, Run, RunStep, Worktree
from gaia.db.repo import ScopedRepo
from gaia.domain.enums import ApprovalKind
from gaia.domain.plans import PlanStep, Review
from gaia.llm.base import LLMUsage, ParsedResponse
from gaia.llm.fake import FakeLLM
from gaia.orchestrator.deps import CheckOutcome, StepOutcome
from gaia.orchestrator.impl import review as review_module
from gaia.orchestrator.impl.review import GatewayReviewer, plan_from_rows
from gaia.services.costs import PriceTable
from gaia.services.git import GitService, WorktreeLocation
from tests.db_builders import make_chain

PLAN_STEP = PlanStep(
    title="Write a test",
    instructions="Add a failing test and make it pass.",
    files_likely_touched=["tests/test_x.py"],
    acceptance=["pytest passes"],
    test_command=["pytest", "-q"],
)

REVIEW = Review(verdict="accept", feedback="looks good", issues=[])


def _parsed_review() -> ParsedResponse[Review]:
    return ParsedResponse(
        output=REVIEW,
        usage=LLMUsage(provider="fake", model="fake-model", input_tokens=10, output_tokens=5),
        model="fake-model",
        request_id=None,
        stop_reason="end_turn",
    )


class FakeGit:
    """Scriptable git service for the reviewer's final diff."""

    def __init__(self) -> None:
        self.diff_calls: list[tuple[Any, str, str]] = []
        self.validate_repo_calls: list[Any] = []
        self.admin_dir_calls: list[tuple[Any, Any]] = []

    async def diff(self, repo: Any, base: str, head: str) -> str:
        self.diff_calls.append((repo, base, head))
        return "diff --git a/a.py b/a.py\n+change\n"

    async def validate_repo(self, path: Any) -> Path:
        self.validate_repo_calls.append(path)
        return Path(path)

    async def worktree_admin_dir(self, repo: Path, work_tree: Path) -> Path:
        self.admin_dir_calls.append((repo, work_tree))
        return Path("/tmp/gitdir")


@asynccontextmanager
async def _fake_lead_llm(
    calls: list[dict[str, Any]],
    **kwargs: Any,
) -> AsyncIterator[FakeLLM]:
    calls.append(kwargs)
    yield FakeLLM([], parsed_responses=[_parsed_review()])


def _make_reviewer(
    database: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    provider: Literal["anthropic", "deepseek"] = "anthropic",
    model: str = "claude-opus-5-5",
    git: GitService | None = None,
) -> tuple[GatewayReviewer, list[dict[str, Any]], FakeGit]:
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(review_module, "lead_llm", lambda **kwargs: _fake_lead_llm(calls, **kwargs))
    fake_git = FakeGit() if git is None else cast(FakeGit, git)
    reviewer = GatewayReviewer(
        settings=Settings(
            planner_provider=provider,
            planner_model=model,
            data_dir=tmp_path / "gaia-data",
        ),
        sessions=database.sessions,
        price_table=PriceTable({}),
        git=cast(GitService, fake_git),
    )
    return reviewer, calls, fake_git


def _plan_step_dict() -> dict[str, Any]:
    return PLAN_STEP.model_dump()


def test_plan_from_rows_uses_steps_and_approval_payload() -> None:
    steps = [
        RunStep(
            run_id="run",
            step_index=0,
            title="t",
            spec_json=_plan_step_dict(),
        )
    ]

    plan = plan_from_rows(
        steps,
        {"kind": "plan", "plan": {"summary": "The summary", "risks": ["risk 1"]}},
    )

    assert plan.summary == "The summary"
    assert plan.risks == ["risk 1"]
    assert plan.steps == [PLAN_STEP]


def test_plan_from_rows_falls_back_when_approval_is_missing() -> None:
    steps = [
        RunStep(
            run_id="run",
            step_index=0,
            title="t",
            spec_json=_plan_step_dict(),
        )
    ]
    plan = plan_from_rows(steps, None)

    assert plan.summary == ""
    assert plan.risks == []
    assert plan.steps == [PLAN_STEP]


def test_plan_from_rows_falls_back_when_plan_payload_is_not_mapping() -> None:
    steps = [
        RunStep(
            run_id="run",
            step_index=0,
            title="t",
            spec_json=_plan_step_dict(),
        )
    ]
    plan = plan_from_rows(steps, {"kind": "plan", "plan": "not-a-plan"})

    assert plan.summary == ""
    assert plan.risks == []
    assert plan.steps == [PLAN_STEP]


async def test_review_step_loads_plan_and_returns_review(
    database: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with session_scope(database.sessions) as session:
        chain = await make_chain(session, slug="review-step")
        steps = ScopedRepo(session, RunStep, chain.workspace.id)
        await steps.update(chain.step.id, spec_json=_plan_step_dict())

    reviewer, calls, _ = _make_reviewer(database, tmp_path, monkeypatch)

    review = await reviewer.review_step(
        chain.run.id,
        0,
        StepOutcome(status="completed", summary="done", files_changed=["a.py"], diff="diff"),
        CheckOutcome(passed=True, output="tests passed"),
    )

    assert review == REVIEW
    assert len(calls) == 1
    assert calls[0]["run_id"] == chain.run.id
    assert calls[0]["workspace_id"] == chain.workspace.id


async def test_review_step_missing_run_raises(
    database: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reviewer, _, _ = _make_reviewer(database, tmp_path, monkeypatch)

    with pytest.raises(ValueError, match="no run"):
        await reviewer.review_step(
            "missing",
            0,
            StepOutcome(status="completed", summary="done", files_changed=[], diff=""),
            CheckOutcome(passed=True, output=""),
        )


async def test_final_review_uses_worktree_base_sha_and_diff(
    database: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with session_scope(database.sessions) as session:
        chain = await make_chain(session, slug="final-review")
        steps = ScopedRepo(session, RunStep, chain.workspace.id)
        await steps.update(chain.step.id, spec_json=_plan_step_dict())
        worktrees = ScopedRepo(session, Worktree, chain.workspace.id)
        await worktrees.update(chain.worktree.id, git_dir=str(tmp_path / "gitdir"))

    reviewer, calls, fake_git = _make_reviewer(database, tmp_path, monkeypatch)

    review = await reviewer.final_review(chain.run.id)

    assert review == REVIEW
    assert len(calls) == 1
    assert len(fake_git.diff_calls) == 1
    location, base, head = fake_git.diff_calls[0]
    assert isinstance(location, WorktreeLocation)
    assert base == chain.worktree.base_sha
    assert head == "HEAD"


async def test_final_review_missing_worktree_raises(
    database: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with session_scope(database.sessions) as session:
        chain = await make_chain(session, slug="final-review-worktree")
        steps = ScopedRepo(session, RunStep, chain.workspace.id)
        await steps.update(chain.step.id, spec_json=_plan_step_dict())
        await ScopedRepo(session, Worktree, chain.workspace.id).update(
            chain.worktree.id, base_sha=None
        )

    reviewer, _, _ = _make_reviewer(database, tmp_path, monkeypatch)

    with pytest.raises(ValueError, match="has no base_sha"):
        await reviewer.final_review(chain.run.id)


async def test_final_review_missing_run_raises(
    database: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reviewer, _, _ = _make_reviewer(database, tmp_path, monkeypatch)

    with pytest.raises(ValueError, match="no run"):
        await reviewer.final_review("missing")


async def test_final_review_run_without_worktree_raises(
    database: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with session_scope(database.sessions) as session:
        chain = await make_chain(session, slug="final-review-no-worktree")
        await ScopedRepo(session, Run, chain.workspace.id).update(chain.run.id, worktree_id=None)

    reviewer, _, _ = _make_reviewer(database, tmp_path, monkeypatch)

    with pytest.raises(ValueError, match="has no worktree"):
        await reviewer.final_review(chain.run.id)


async def test_final_review_worktree_row_from_another_workspace_raises(
    database: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with session_scope(database.sessions) as session:
        chain = await make_chain(session, slug="final-review-worktree-row")
        other_chain = await make_chain(session, slug="other-final-worktree-row")
        await ScopedRepo(session, Run, chain.workspace.id).update(
            chain.run.id, worktree_id=other_chain.worktree.id
        )

    reviewer, _, _ = _make_reviewer(database, tmp_path, monkeypatch)

    with pytest.raises(ValueError, match="has no worktree"):
        await reviewer.final_review(chain.run.id)


async def test_final_review_repository_row_from_another_workspace_raises(
    database: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with session_scope(database.sessions) as session:
        chain = await make_chain(session, slug="final-review-repo-row")
        other_chain = await make_chain(session, slug="other-final-repo-row")
        await ScopedRepo(session, Run, chain.workspace.id).update(
            chain.run.id, repo_id=other_chain.repository.id
        )

    reviewer, _, _ = _make_reviewer(database, tmp_path, monkeypatch)

    with pytest.raises(ValueError, match="has no repository"):
        await reviewer.final_review(chain.run.id)


async def test_final_review_derives_git_dir_when_row_has_none(
    database: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with session_scope(database.sessions) as session:
        chain = await make_chain(session, slug="final-review-derive")
        steps = ScopedRepo(session, RunStep, chain.workspace.id)
        await steps.update(chain.step.id, spec_json=_plan_step_dict())

    reviewer, _, fake_git = _make_reviewer(database, tmp_path, monkeypatch)

    await reviewer.final_review(chain.run.id)

    assert len(fake_git.validate_repo_calls) == 1
    assert len(fake_git.admin_dir_calls) == 1


async def test_final_review_uses_most_recent_plan_approval(
    database: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with session_scope(database.sessions) as session:
        chain = await make_chain(session, slug="final-review-approval")
        steps = ScopedRepo(session, RunStep, chain.workspace.id)
        await steps.update(chain.step.id, spec_json=_plan_step_dict())
        approvals = ScopedRepo(session, Approval, chain.workspace.id)
        first = await approvals.create(
            run_id=chain.run.id,
            idempotency_key="approve_plan:0:1",
            kind=ApprovalKind.PLAN.value,
            payload_json={"kind": "plan", "plan": {"summary": "Old", "risks": []}},
        )
        await approvals.update(first.id, created_at=datetime(2026, 1, 1, tzinfo=UTC))
        await approvals.create(
            run_id=chain.run.id,
            idempotency_key="approve_plan:0:2",
            kind=ApprovalKind.PLAN.value,
            payload_json={"kind": "plan", "plan": {"summary": "New", "risks": ["r"]}},
        )

    reviewer, _, _ = _make_reviewer(database, tmp_path, monkeypatch)

    async with session_scope(database.sessions) as session:
        plan = await reviewer._plan_for_run(session, chain.workspace.id, chain.run.id)

    assert plan.summary == "New"
    assert plan.risks == ["r"]
    assert plan.steps == [PLAN_STEP]
