"""``prepare_worktree``: create or reuse the run's worktree (doc 12a)."""

from __future__ import annotations

from typing import Any

from gaia.db.engine import session_scope
from gaia.domain.enums import RunStatus
from gaia.domain.state import transition
from gaia.orchestrator.deps import GraphDeps
from gaia.orchestrator.nodes._common import load_run
from gaia.orchestrator.state import RunState


async def prepare_worktree(state: RunState, deps: GraphDeps) -> dict[str, Any]:
    """Prepare the worktree and set the run to ``planning``.

    The preparer runs first, outside this node's database session: it performs
    git, sandbox and setup-command work, which must never happen inside a DB
    transaction (lead lesson from 15a).
    """
    worktree_id = await deps.worktrees.prepare(state["run_id"])
    async with session_scope(deps.sessions) as session:
        run = await load_run(session, state)
        transition(run, RunStatus.PLANNING)
        run.worktree_id = worktree_id
    return {"worktree_id": worktree_id}
