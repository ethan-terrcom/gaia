"""The LangGraph state schema for one ticket run (doc 12a).

The state is deliberately JSON-safe so the checkpointer can persist it with the
default serializer. Values are plain dicts and scalars, never SQLAlchemy rows or
service objects.

One extra key, ``route``, is a string that interrupting/routing nodes set so the
graph's conditional edges can choose the next node without the router reaching
into the database. It is JSON-safe and has the default "last value wins"
semantics, so it needs no reducer.
"""

from __future__ import annotations

from typing import Any, TypedDict


class RunState(TypedDict, total=False):
    """State for a single ``thread_id = run_id`` graph execution."""

    run_id: str
    ticket_id: str
    repo_id: str
    workspace_id: str
    worktree_id: str | None
    plan: dict[str, Any] | None
    step_index: int
    attempt: int
    feedback: str | None
    step_results: list[dict[str, Any]]
    review: dict[str, Any] | None
    pr_number: int | None
    error: str | None
    #: Set by nodes immediately before a conditional edge; the edge's path
    #: function simply returns this value.
    route: str | None
