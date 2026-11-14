"""The durable ticket-run graph (doc 12a)."""

from __future__ import annotations

from collections.abc import Hashable
from functools import partial
from typing import Any, cast

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Checkpointer

from gaia.orchestrator.deps import GraphDeps
from gaia.orchestrator.guard import with_guard
from gaia.orchestrator.nodes.approve_merge import approve_merge
from gaia.orchestrator.nodes.approve_plan import approve_plan
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

Node = Any


def _route(state: RunState) -> str:
    """Return the route a node just stored (``"end"`` when it stored none).

    Every conditional edge passes an explicit path map (see :func:`_paths`).
    Without one, LangGraph silently drops a route to an unknown node, so a
    typo'd route name would quietly stop a run mid-workflow; with the map it
    raises. The maps also make the conditional edges visible to
    ``draw_mermaid`` (lead review 12a, R5).
    """
    return state.get("route") or "end"


def _paths(*nodes: str) -> dict[Hashable, str]:
    """A path map: each route name to the node of that name, plus ``"end"``."""
    paths: dict[Hashable, str] = {"end": END}
    paths.update({node: node for node in nodes})
    return paths


def build_graph(
    deps: GraphDeps, *, checkpointer: Checkpointer = None
) -> CompiledStateGraph[RunState, None, RunState, RunState]:
    """Build and compile the 12a skeleton graph.

    The optional ``checkpointer`` is how tests (and 12b's lifespan) attach the
    real ``AsyncSqliteSaver``; the default ``None`` keeps the call compatible
    with the doc's ``build_graph(deps)`` shorthand.
    """
    graph = StateGraph(RunState)

    def add(name: str, node: Node) -> None:
        wrapped = with_guard(deps, name)(cast(Any, partial(node, deps=deps)))
        # LangGraph's published StateNode protocol is wider than the callable
        # type ``with_guard`` returns; the wrapper's input/output are known.
        graph.add_node(name, cast(Any, wrapped))

    add("prepare_worktree", prepare_worktree)
    add("lead_plan", lead_plan)
    add("approve_plan", approve_plan)
    add("implement_step", implement_step)
    add("run_checks", run_checks)
    add("lead_review_step", lead_review_step)
    add("git_commit_step", git_commit_step)
    add("lead_final_review", lead_final_review)
    add("approve_pr", approve_pr)
    add("open_pr", open_pr)
    add("watch_ci", watch_ci)
    add("approve_merge", approve_merge)
    add("merge", merge)
    add("escalation", escalation)
    add("finish", finish)

    graph.add_edge(START, "prepare_worktree")
    graph.add_edge("prepare_worktree", "lead_plan")
    graph.add_edge("lead_plan", "approve_plan")
    graph.add_conditional_edges("approve_plan", _route, _paths("implement_step"))

    graph.add_edge("implement_step", "run_checks")
    graph.add_edge("run_checks", "lead_review_step")
    graph.add_conditional_edges(
        "lead_review_step", _route, _paths("git_commit_step", "implement_step", "escalation")
    )
    graph.add_conditional_edges(
        "git_commit_step", _route, _paths("implement_step", "lead_final_review")
    )
    graph.add_conditional_edges("escalation", _route, _paths("implement_step", "watch_ci"))

    graph.add_edge("lead_final_review", "approve_pr")
    graph.add_conditional_edges("approve_pr", _route, _paths("open_pr"))

    graph.add_edge("open_pr", "watch_ci")
    graph.add_conditional_edges("watch_ci", _route, _paths("approve_merge", "escalation"))
    graph.add_conditional_edges("approve_merge", _route, _paths("merge"))

    graph.add_edge("merge", "finish")
    graph.add_edge("finish", END)

    return graph.compile(checkpointer=checkpointer)
