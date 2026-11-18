"""Direct unit tests for the 12a graph nodes (no LangGraph execution)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from gaia.config.settings import Settings
from gaia.db.engine import session_scope
from gaia.db.ids import uuid7
from gaia.db.models import Run, RunStep, Ticket
from gaia.db.repo import ScopedRepo
from gaia.domain.enums import RunStatus, RunStepStatus, TicketStatus
from gaia.domain.plans import Plan
from gaia.domain.state import transition
from gaia.orchestrator.deps import CheckOutcome, GraphDeps
from gaia.orchestrator.nodes._common import current_step_result, load_run, load_ticket
from gaia.orchestrator.nodes.approve_merge import approve_merge
from gaia.orchestrator.nodes.approve_plan import _replace_pending_steps, approve_plan
from gaia.orchestrator.nodes.approve_pr import approve_pr
from gaia.orchestrator.nodes.escalation import escalation
from gaia.orchestrator.nodes.finish import finish
from gaia.orchestrator.nodes.git_commit_step import git_commit_step
from gaia.orchestrator.nodes.implement_step import implement_step
from gaia.orchestrator.nodes.lead_final_review import lead_final_review
from gaia.orchestrator.nodes.lead_plan import lead_plan
from gaia.orchestrator.nodes.lead_review_step import lead_review_step
from gaia.orchestrator.nodes.merge import merge
from gaia.orchestrator.nodes.open_pr import open_pr
from gaia.orchestrator.nodes.prepare_worktree import prepare_worktree
from gaia.orchestrator.nodes.run_checks import run_checks
from gaia.orchestrator.nodes.watch_ci import watch_ci
from gaia.orchestrator.state import RunState
from tests.orchestrator_fakes import (
    FakeReviewer,
    make_check_outcome,
    make_outcome,
    make_plan,
    make_review,
)
from tests.orchestrator_helpers import Fakes, make_fakes, make_graph_deps, make_ticket_parents


@dataclass(frozen=True)
class NodeEnv:
    deps: GraphDeps
    fakes: Fakes
    settings: Settings
    workspace_id: str
    user_id: str
    ticket_id: str
    repo_id: str
    run_id: str


def base_state(env: NodeEnv, **overrides: Any) -> RunState:
    state: RunState = {
        "run_id": env.run_id,
        "ticket_id": env.ticket_id,
        "repo_id": env.repo_id,
        "workspace_id": env.workspace_id,
        "worktree_id": None,
        "plan": None,
        "step_index": 0,
        "attempt": 1,
        "feedback": None,
        "step_results": [],
        "review": None,
        "pr_number": None,
        "error": None,
        "route": None,
    }
    state.update(overrides)  # type: ignore[typeddict-item]
    return state


@asynccontextmanager
async def node_env(
    database: Any, tmp_path: Path, fakes: Fakes | None = None
) -> AsyncIterator[NodeEnv]:
    """Create the rows and dependencies needed to call one node directly."""
    settings = Settings(data_dir=tmp_path / "gaia-data")
    fakes = fakes or make_fakes()
    deps = make_graph_deps(database, settings, fakes)
    async with session_scope(database.sessions) as session:
        parents = await make_ticket_parents(session)
        run_id = uuid7()
        runs = ScopedRepo(session, Run, parents.workspace.id)
        await runs.create(
            id=run_id,
            ticket_id=parents.ticket.id,
            repo_id=parents.repository.id,
            budget_usd=Decimal("10.0"),
            graph_thread_id=run_id,
            created_by=parents.user.id,
        )
    yield NodeEnv(
        deps=deps,
        fakes=fakes,
        settings=settings,
        workspace_id=parents.workspace.id,
        user_id=parents.user.id,
        ticket_id=parents.ticket.id,
        repo_id=parents.repository.id,
        run_id=run_id,
    )


async def set_run_status(env: NodeEnv, *statuses: RunStatus) -> None:
    async with session_scope(env.deps.sessions) as session:
        run = await ScopedRepo(session, Run, env.workspace_id).get(env.run_id)
        assert run is not None
        for status in statuses:
            transition(run, status)


async def set_ticket_status(env: NodeEnv, *statuses: TicketStatus) -> None:
    async with session_scope(env.deps.sessions) as session:
        ticket = await ScopedRepo(session, Ticket, env.workspace_id).get(env.ticket_id)
        assert ticket is not None
        for status in statuses:
            transition(ticket, status)


async def set_step_status(env: NodeEnv, step_index: int, status: RunStepStatus) -> None:
    async with session_scope(env.deps.sessions) as session:
        steps = ScopedRepo(session, RunStep, env.workspace_id)
        step_row = next(
            iter(await steps.list(run_id=env.run_id, step_index=step_index, limit=1)), None
        )
        assert step_row is not None
        transition(step_row, status)


async def add_run_steps(env: NodeEnv, plan: Plan) -> None:
    async with session_scope(env.deps.sessions) as session:
        steps = ScopedRepo(session, RunStep, env.workspace_id)
        for index, step in enumerate(plan.steps):
            await steps.create(
                run_id=env.run_id,
                step_index=index,
                title=step.title,
                spec_json=step.model_dump(),
            )


async def test_prepare_worktree_sets_run_planning(database: Any, tmp_path: Path) -> None:
    async with node_env(database, tmp_path) as env:
        result = await prepare_worktree(base_state(env), env.deps)
        assert result == {"worktree_id": "worktree-1"}
        assert env.fakes.worktrees.calls == [env.run_id]
        async with session_scope(env.deps.sessions) as session:
            run = await ScopedRepo(session, Run, env.workspace_id).get(env.run_id)
            assert run is not None
            assert run.status == RunStatus.PLANNING.value
            assert run.worktree_id == "worktree-1"


async def test_lead_plan_persists_steps_idempotently(database: Any, tmp_path: Path) -> None:
    plan = make_plan(2)
    fakes = make_fakes(plan=plan)
    async with node_env(database, tmp_path, fakes) as env:
        first = await lead_plan(base_state(env), env.deps)
        second = await lead_plan(base_state(env, plan=first["plan"]), env.deps)
        assert first["plan"]["summary"] == plan.summary
        assert second == first
        async with session_scope(env.deps.sessions) as session:
            steps = await ScopedRepo(session, RunStep, env.workspace_id).list(run_id=env.run_id)
            assert len(steps) == 2


async def test_implement_step_moves_ticket_and_stores_result(database: Any, tmp_path: Path) -> None:
    plan = make_plan(1)
    fakes = make_fakes(plan=plan)
    async with node_env(database, tmp_path, fakes) as env:
        await add_run_steps(env, plan)
        await set_run_status(env, RunStatus.PLANNING)
        state = base_state(env, plan=plan.model_dump())
        result = await implement_step(state, env.deps)
        assert result["feedback"] is None
        assert len(result["step_results"]) == 1
        async with session_scope(env.deps.sessions) as session:
            ticket = await ScopedRepo(session, Ticket, env.workspace_id).get(env.ticket_id)
            assert ticket is not None
            assert ticket.status == TicketStatus.IN_PROGRESS.value
            steps = await ScopedRepo(session, RunStep, env.workspace_id).list(run_id=env.run_id)
            assert steps[0].status == RunStepStatus.RUNNING.value
            assert steps[0].attempts == 1


async def test_run_checks_attaches_check_to_latest_result(database: Any, tmp_path: Path) -> None:
    plan = make_plan(1)
    fakes = make_fakes(plan=plan)
    async with node_env(database, tmp_path, fakes) as env:
        await add_run_steps(env, plan)
        await set_run_status(env, RunStatus.PLANNING)
        state = base_state(env, plan=plan.model_dump())
        implemented = await implement_step(state, env.deps)
        checked = await run_checks(
            base_state(env, plan=plan.model_dump(), step_results=implemented["step_results"]),
            env.deps,
        )
        assert checked["step_results"][-1]["checks"]["passed"] is True


async def test_run_checks_requires_a_step_result(database: Any, tmp_path: Path) -> None:
    async with node_env(database, tmp_path) as env:
        await set_run_status(env, RunStatus.PLANNING)
        with pytest.raises(ValueError, match="requires an implementation outcome"):
            await run_checks(base_state(env), env.deps)


async def test_lead_review_step_routes_revise_and_escalate(database: Any, tmp_path: Path) -> None:
    plan = make_plan(1)
    reviewer = FakeReviewer(
        step_review_sequence=[
            make_review("revise", "try again"),
            make_review("escalate", "need owner"),
        ]
    )
    fakes = make_fakes(plan=plan, reviewer=reviewer)
    async with node_env(database, tmp_path, fakes) as env:
        await add_run_steps(env, plan)
        await set_run_status(env, RunStatus.PLANNING, RunStatus.IMPLEMENTING)
        step_result = {
            "step_index": 0,
            "attempt": 1,
            "outcome": make_outcome().__dict__,
            "checks": make_check_outcome().__dict__,
        }
        state = base_state(env, plan=plan.model_dump(), step_results=[step_result])
        revised = await lead_review_step(state, env.deps)
        assert revised["route"] == "implement_step"
        assert revised["attempt"] == 2
        assert revised["feedback"] == "try again"

        state = base_state(
            env,
            plan=plan.model_dump(),
            step_results=[step_result],
            attempt=3,
        )
        escalated = await lead_review_step(state, env.deps)
        assert escalated["route"] == "escalation"
        assert escalated["error"] == "step_escalation"


async def test_lead_review_step_requires_result(database: Any, tmp_path: Path) -> None:
    async with node_env(database, tmp_path) as env:
        await set_run_status(env, RunStatus.PLANNING, RunStatus.IMPLEMENTING)
        with pytest.raises(ValueError, match="requires an outcome"):
            await lead_review_step(base_state(env), env.deps)


async def test_git_commit_step_advances_or_finishes(database: Any, tmp_path: Path) -> None:
    plan = make_plan(2)
    async with node_env(database, tmp_path, make_fakes(plan=plan)) as env:
        await add_run_steps(env, plan)
        await set_run_status(env, RunStatus.PLANNING, RunStatus.IMPLEMENTING)
        await set_step_status(env, 0, RunStepStatus.RUNNING)
        await set_step_status(env, 1, RunStepStatus.RUNNING)
        state = base_state(env, plan=plan.model_dump())
        first = await git_commit_step(state, env.deps)
        assert first["route"] == "implement_step"
        assert first["step_index"] == 1

        second = await git_commit_step(
            base_state(env, plan=plan.model_dump(), step_index=1), env.deps
        )
        assert second["route"] == "lead_final_review"

        async with session_scope(env.deps.sessions) as session:
            steps = await ScopedRepo(session, RunStep, env.workspace_id).list(run_id=env.run_id)
            assert [step.status for step in steps] == [
                RunStepStatus.COMPLETED.value,
                RunStepStatus.COMPLETED.value,
            ]


async def test_git_commit_step_requires_plan(database: Any, tmp_path: Path) -> None:
    async with node_env(database, tmp_path) as env:
        await set_run_status(env, RunStatus.PLANNING, RunStatus.IMPLEMENTING)
        with pytest.raises(ValueError, match="requires a plan"):
            await git_commit_step(base_state(env), env.deps)


async def test_lead_final_review_records_review(database: Any, tmp_path: Path) -> None:
    async with node_env(database, tmp_path) as env:
        await set_run_status(env, RunStatus.PLANNING, RunStatus.IMPLEMENTING)
        result = await lead_final_review(base_state(env), env.deps)
        assert result["review"]["verdict"] == "accept"
        assert env.fakes.reviewer.final_review_calls == [env.run_id]
        assert env.fakes.reviewer.final_review_checks == [{}]


async def test_lead_final_review_passes_each_steps_latest_checks(
    database: Any, tmp_path: Path
) -> None:
    def result(step_index: int, attempt: int, checks: CheckOutcome | None) -> dict[str, Any]:
        return {
            "step_index": step_index,
            "attempt": attempt,
            "outcome": None,
            "checks": None if checks is None else asdict(checks),
        }

    first_try = CheckOutcome(passed=False, output="1 failed")
    second_try = CheckOutcome(passed=True, output="3 passed")
    last_step = CheckOutcome(passed=True, output="5 passed")
    async with node_env(database, tmp_path) as env:
        await set_run_status(env, RunStatus.PLANNING, RunStatus.IMPLEMENTING)
        state = base_state(
            env,
            step_results=[
                result(0, 1, first_try),
                result(0, 2, second_try),
                result(1, 1, last_step),
                result(2, 1, None),
            ],
        )
        await lead_final_review(state, env.deps)
        assert env.fakes.reviewer.final_review_checks == [{0: second_try, 1: last_step}]


async def test_open_pr_moves_ticket_to_review(database: Any, tmp_path: Path) -> None:
    async with node_env(database, tmp_path) as env:
        await set_run_status(env, RunStatus.PLANNING, RunStatus.AWAITING_APPROVAL)
        await set_ticket_status(env, TicketStatus.READY, TicketStatus.IN_PROGRESS)
        result = await open_pr(base_state(env), env.deps)
        assert result["pr_number"] == 42
        async with session_scope(env.deps.sessions) as session:
            ticket = await ScopedRepo(session, Ticket, env.workspace_id).get(env.ticket_id)
            assert ticket is not None
            assert ticket.status == TicketStatus.IN_REVIEW.value


async def test_merge_moves_ticket_to_done(database: Any, tmp_path: Path) -> None:
    async with node_env(database, tmp_path) as env:
        await set_run_status(env, RunStatus.PLANNING, RunStatus.AWAITING_APPROVAL)
        await set_ticket_status(env, TicketStatus.READY, TicketStatus.IN_PROGRESS)
        await open_pr(base_state(env), env.deps)
        result = await merge(base_state(env, pr_number=42), env.deps)
        assert result == {"error": None}
        async with session_scope(env.deps.sessions) as session:
            ticket = await ScopedRepo(session, Ticket, env.workspace_id).get(env.ticket_id)
            assert ticket is not None
            assert ticket.status == TicketStatus.DONE.value


async def test_merge_requires_pr_number(database: Any, tmp_path: Path) -> None:
    async with node_env(database, tmp_path) as env:
        await set_run_status(
            env, RunStatus.PLANNING, RunStatus.AWAITING_APPROVAL, RunStatus.PR_OPEN
        )
        with pytest.raises(ValueError, match="requires pr_number"):
            await merge(base_state(env), env.deps)


async def test_watch_ci_failed_routes_to_escalation(database: Any, tmp_path: Path) -> None:
    fakes = make_fakes()
    fakes.pull_requests.ci_results[42] = "failed"
    async with node_env(database, tmp_path, fakes) as env:
        await set_run_status(
            env, RunStatus.PLANNING, RunStatus.AWAITING_APPROVAL, RunStatus.PR_OPEN
        )
        result = await watch_ci(base_state(env, pr_number=42), env.deps)
        assert result["route"] == "escalation"
        assert result["error"] == "ci_failed"


async def test_watch_ci_requires_pr_number(database: Any, tmp_path: Path) -> None:
    async with node_env(database, tmp_path) as env:
        await set_run_status(
            env, RunStatus.PLANNING, RunStatus.AWAITING_APPROVAL, RunStatus.PR_OPEN
        )
        with pytest.raises(ValueError, match="requires pr_number"):
            await watch_ci(base_state(env), env.deps)


async def test_finish_sets_run_completed(database: Any, tmp_path: Path) -> None:
    async with node_env(database, tmp_path) as env:
        await set_run_status(
            env, RunStatus.PLANNING, RunStatus.AWAITING_APPROVAL, RunStatus.PR_OPEN
        )
        result = await finish(base_state(env), env.deps)
        assert result == {"error": None}
        async with session_scope(env.deps.sessions) as session:
            run = await ScopedRepo(session, Run, env.workspace_id).get(env.run_id)
            assert run is not None
            assert run.status == RunStatus.COMPLETED.value


async def test_approve_plan_edit_replaces_steps(database: Any, tmp_path: Path) -> None:
    plan = make_plan(2)
    fakes = make_fakes(plan=plan)
    async with node_env(database, tmp_path, fakes) as env:
        await add_run_steps(env, plan)
        await set_run_status(env, RunStatus.PLANNING)
        new_plan = make_plan(1)
        import gaia.orchestrator.nodes.approve_plan as approve_plan_module

        monkeypatch = pytest.MonkeyPatch()
        monkeypatch.setattr(
            approve_plan_module,
            "interrupt",
            lambda payload: {"action": "edit", "plan": new_plan.model_dump()},
        )
        result = await approve_plan(base_state(env, plan=plan.model_dump()), env.deps)
        monkeypatch.undo()
        assert result["route"] == "implement_step"
        async with session_scope(env.deps.sessions) as session:
            steps = await ScopedRepo(session, RunStep, env.workspace_id).list(run_id=env.run_id)
            assert len(steps) == 1


async def test_approve_plan_reject_cancels_run(database: Any, tmp_path: Path) -> None:
    plan = make_plan(1)
    async with node_env(database, tmp_path, make_fakes(plan=plan)) as env:
        await set_run_status(env, RunStatus.PLANNING)
        import gaia.orchestrator.nodes.approve_plan as approve_plan_module

        monkeypatch = pytest.MonkeyPatch()
        monkeypatch.setattr(approve_plan_module, "interrupt", lambda payload: {"action": "reject"})
        result = await approve_plan(base_state(env, plan=plan.model_dump()), env.deps)
        monkeypatch.undo()
        assert result["route"] == "end"
        assert result["error"] == "plan_rejected"
        async with session_scope(env.deps.sessions) as session:
            run = await ScopedRepo(session, Run, env.workspace_id).get(env.run_id)
            assert run is not None
            assert run.status == RunStatus.CANCELLED.value


async def test_approve_pr_reject_cancels_run(database: Any, tmp_path: Path) -> None:
    async with node_env(database, tmp_path) as env:
        await set_run_status(env, RunStatus.PLANNING, RunStatus.AWAITING_APPROVAL)
        import gaia.orchestrator.nodes.approve_pr as approve_pr_module

        monkeypatch = pytest.MonkeyPatch()
        monkeypatch.setattr(approve_pr_module, "interrupt", lambda payload: {"action": "reject"})
        result = await approve_pr(base_state(env), env.deps)
        monkeypatch.undo()
        assert result["route"] == "end"
        async with session_scope(env.deps.sessions) as session:
            run = await ScopedRepo(session, Run, env.workspace_id).get(env.run_id)
            assert run is not None
            assert run.status == RunStatus.CANCELLED.value


async def test_approve_merge_reject_cancels_run(database: Any, tmp_path: Path) -> None:
    async with node_env(database, tmp_path) as env:
        await set_run_status(env, RunStatus.PLANNING, RunStatus.AWAITING_APPROVAL)
        import gaia.orchestrator.nodes.approve_merge as approve_merge_module

        monkeypatch = pytest.MonkeyPatch()
        monkeypatch.setattr(approve_merge_module, "interrupt", lambda payload: {"action": "reject"})
        result = await approve_merge(base_state(env), env.deps)
        monkeypatch.undo()
        assert result["route"] == "end"
        async with session_scope(env.deps.sessions) as session:
            run = await ScopedRepo(session, Run, env.workspace_id).get(env.run_id)
            assert run is not None
            assert run.status == RunStatus.CANCELLED.value


async def test_escalation_ci_failed_approve_routes_back_to_watch_ci(
    database: Any, tmp_path: Path
) -> None:
    async with node_env(database, tmp_path) as env:
        await set_run_status(env, RunStatus.PLANNING, RunStatus.AWAITING_APPROVAL)
        import gaia.orchestrator.nodes.escalation as escalation_module

        monkeypatch = pytest.MonkeyPatch()
        monkeypatch.setattr(escalation_module, "interrupt", lambda payload: {"action": "approve"})
        result = await escalation(base_state(env, error="ci_failed", pr_number=42), env.deps)
        monkeypatch.undo()
        assert result["route"] == "watch_ci"
        assert result["error"] is None


async def test_common_loaders_report_missing_rows(database: Any, tmp_path: Path) -> None:
    async with node_env(database, tmp_path) as env:
        missing_run = base_state(env, run_id="missing-run")
        with pytest.raises(ValueError, match="no run"):
            async with session_scope(env.deps.sessions) as session:
                await load_run(session, missing_run)

        missing_ticket = base_state(env, ticket_id="missing-ticket")
        with pytest.raises(ValueError, match="no ticket"):
            async with session_scope(env.deps.sessions) as session:
                await load_ticket(session, missing_ticket)


def test_current_step_result_returns_none_without_results() -> None:
    assert current_step_result({"step_results": [], "step_index": 0}) is None


def test_current_step_result_skips_results_for_other_steps() -> None:
    assert (
        current_step_result({"step_results": [{"step_index": 1, "outcome": {}}], "step_index": 0})
        is None
    )


async def test_implement_step_without_a_run_step_row_still_records_the_outcome(
    database: Any, tmp_path: Path
) -> None:
    plan = make_plan(1)
    async with node_env(database, tmp_path, make_fakes(plan=plan)) as env:
        await set_run_status(env, RunStatus.PLANNING)
        result = await implement_step(base_state(env, plan=plan.model_dump()), env.deps)
        assert len(result["step_results"]) == 1
        async with session_scope(env.deps.sessions) as session:
            rows = await ScopedRepo(session, RunStep, env.workspace_id).list(run_id=env.run_id)
            assert rows == []


async def test_git_commit_step_requires_plan_steps_to_be_a_list(
    database: Any, tmp_path: Path
) -> None:
    async with node_env(database, tmp_path) as env:
        with pytest.raises(ValueError, match=r"plan\.steps must be a list"):
            await git_commit_step(base_state(env, plan={}), env.deps)


async def test_git_commit_step_requires_step_index_to_be_inside_the_plan(
    database: Any, tmp_path: Path
) -> None:
    plan = make_plan(1)
    async with node_env(database, tmp_path) as env:
        with pytest.raises(ValueError, match="step_index is outside the plan"):
            await git_commit_step(base_state(env, plan=plan.model_dump(), step_index=1), env.deps)


async def test_git_commit_step_without_a_run_step_row_still_advances(
    database: Any, tmp_path: Path
) -> None:
    plan = make_plan(2)
    async with node_env(database, tmp_path, make_fakes(plan=plan)) as env:
        await set_run_status(env, RunStatus.PLANNING, RunStatus.IMPLEMENTING)
        result = await git_commit_step(
            base_state(env, plan=plan.model_dump(), step_index=0), env.deps
        )
        assert result["route"] == "implement_step"
        assert result["step_index"] == 1
        assert env.fakes.committer.calls == [(env.run_id, 0, "Step 1")]


async def test_replace_pending_steps_keeps_completed_steps(database: Any, tmp_path: Path) -> None:
    plan = make_plan(2)
    replacement = make_plan(1)
    async with node_env(database, tmp_path) as env:
        await add_run_steps(env, plan)
        await set_step_status(env, 1, RunStepStatus.RUNNING)
        await set_step_status(env, 1, RunStepStatus.COMPLETED)

        async with session_scope(env.deps.sessions) as session:
            await _replace_pending_steps(session, base_state(env), replacement)

        async with session_scope(env.deps.sessions) as session:
            rows = await ScopedRepo(session, RunStep, env.workspace_id).list(run_id=env.run_id)
            by_index = {row.step_index: row.status for row in rows}
            assert by_index == {
                0: RunStepStatus.PENDING.value,
                1: RunStepStatus.COMPLETED.value,
            }
