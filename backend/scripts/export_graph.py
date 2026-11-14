"""Write the 12a LangGraph topology to ``docs/graph.md``.

Run from ``backend/`` with ``uv run python scripts/export_graph.py`` (or
``make graph`` from the repository root). The graph is compiled with no
checkpointer and dummy dependencies: :func:`build_graph` only needs them while a
node runs, and this script only asks the compiled graph for its Mermaid diagram.
"""

from __future__ import annotations

from pathlib import Path
from typing import cast

from gaia.orchestrator.deps import GraphDeps
from gaia.orchestrator.graph import build_graph

#: ``backend/scripts`` → repository root → docs.
TARGET = Path(__file__).resolve().parents[2] / "docs" / "graph.md"

_TITLE = "# Orchestrator graph\n"
_INTRO = (
    "The Mermaid diagram below is the 12a ticket-run topology; the node-by-node\n"
    "behaviour is specified in `ARCHITECTURE.md` §4.\n"
)


def main() -> None:
    """Render the graph and write the Markdown document."""
    deps = cast(GraphDeps, None)
    graph = build_graph(deps)
    mermaid = graph.get_graph().draw_mermaid()
    TARGET.write_text(f"{_TITLE}\n{_INTRO}\n```mermaid\n{mermaid}\n```\n")


if __name__ == "__main__":
    main()
