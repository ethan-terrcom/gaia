"""The ``finish`` call every agent runtime ends a task with (docs 10b, 10c).

``finish`` is runtime protocol, not a registry tool: it never touches the worktree, so it does not
go through :mod:`gaia.tools.registry`. Both the native runtime and the Claude Agent SDK runtime
offer it under the same name with the same JSON schema, so one system prompt works for either
(ADR-001 P3, P5). Its arguments become ``AgentResult.structured_output``.

``files_changed`` is deliberately absent: the runtime never trusts the agent's claim, and
``run_with_limits`` asks git instead (doc 10, requirement 6).
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

FINISH_TOOL_NAME = "finish"

FINISH_TOOL_DESCRIPTION = (
    "End the task. Call this exactly once, after running the step's tests. "
    "Report honestly: tests_passed is false if any test failed or was not run."
)


class FinishArgs(BaseModel):
    """What the agent reports when it ends a task."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    summary: str = Field(min_length=1, max_length=4000, description="What you changed and why.")
    tests_run: list[str] = Field(
        default_factory=list, max_length=50, description="The test commands you ran."
    )
    tests_passed: bool = Field(description="True only if every test you ran passed.")
    concerns: list[str] = Field(
        default_factory=list,
        max_length=20,
        description="Anything the reviewer should know: scope changes, risks, unfinished work.",
    )


def finish_input_schema() -> dict[str, Any]:
    """The JSON schema both runtimes advertise for ``finish``."""
    return FinishArgs.model_json_schema()
