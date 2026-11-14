"""``lead_review_step``: review one implementation attempt and route it."""

from __future__ import annotations

from typing import Any

from gaia.db.engine import session_scope
from gaia.domain.enums import RunStatus
from gaia.orchestrator.deps import CheckOutcome, GraphDeps, StepOutcome
from gaia.orchestrator.nodes._common import current_step_result, set_run_status
from gaia.orchestrator.state import RunState


async def lead_review_step(state: RunState, deps: GraphDeps) -> dict[str, Any]:
    """Review the current step and decide whether to accept, revise or escalate."""
    async with session_scope(deps.sessions) as session:
        await set_run_status(session, state, RunStatus.REVIEWING)

    result = current_step_result(state)
    if result is None or result.get("outcome") is None or result.get("checks") is None:
        raise ValueError("lead_review_step requires an outcome and checks for the current step")
    outcome = StepOutcome(**result["outcome"])
    checks = CheckOutcome(**result["checks"])

    review = await deps.reviewer.review_step(
        state["run_id"],
        state["step_index"],
        outcome,
        checks,
    )
    review_dict = review.model_dump()
    attempt = state.get("attempt", 1)

    if review.verdict == "accept":
        return {"review": review_dict, "route": "git_commit_step", "error": None}
    if review.verdict == "revise" and attempt < 3:
        return {
            "review": review_dict,
            "attempt": attempt + 1,
            "feedback": review.feedback,
            "route": "implement_step",
            "error": None,
        }
    return {
        "review": review_dict,
        "feedback": review.feedback,
        "route": "escalation",
        "error": "step_escalation",
    }
