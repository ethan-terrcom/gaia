"""``lead_final_review``: the lead's review of the complete diff."""

from __future__ import annotations

from typing import Any

from gaia.db.engine import session_scope
from gaia.domain.enums import RunStatus
from gaia.orchestrator.deps import CheckOutcome, GraphDeps
from gaia.orchestrator.nodes._common import set_run_status
from gaia.orchestrator.state import RunState


def latest_checks(state: RunState) -> dict[int, CheckOutcome]:
    """Each step's checks from its latest attempt that ran them.

    ``step_results`` is in attempt order, so a later attempt's checks replace
    an earlier one's: the result is what each accepted step was reviewed with.
    """
    checks: dict[int, CheckOutcome] = {}
    for result in state.get("step_results") or []:
        raw = result.get("checks")
        if raw is not None:
            checks[result["step_index"]] = CheckOutcome(**raw)
    return checks


async def lead_final_review(state: RunState, deps: GraphDeps) -> dict[str, Any]:
    """Run the final review with the step checks and store the verdict."""
    async with session_scope(deps.sessions) as session:
        await set_run_status(session, state, RunStatus.REVIEWING)

    review = await deps.reviewer.final_review(state["run_id"], latest_checks(state))
    return {"review": review.model_dump(), "error": None}
