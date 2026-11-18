"""The ``AgentRuntime`` seam every runtime implements (doc 10a).

The interface is deliberately tiny: a runtime receives an :class:`AgentTask`, may
stream progress through the :class:`EventSink` protocol from
:mod:`gaia.services.events`, and returns an :class:`AgentResult`. Shared
behaviour that must be identical in every runtime - timeout, cancellation,
``files_changed`` - lives in :mod:`gaia.runtimes.support`, not in the runtimes.

The registry is a plain object built during the application lifespan (or by a
CLI command). It is not a module global: tests and nested apps build their own,
so two runtimes with the same name can never collide across a process.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict

from gaia.domain.enums import AgentRole
from gaia.services.events import EventSink
from gaia.services.git import WorktreeLocation

RunStatus = Literal["completed", "failed", "timeout", "cancelled", "budget_exceeded"]


class AgentTask(BaseModel):
    """Everything one runtime call needs, validated before it reaches a runtime.

    ``worktree`` is a :class:`~gaia.services.git.WorktreeLocation` rather than a
    path: git commands inside a worktree must use the trusted admin directory
    (doc 05b, finding F1), and making the type a plain path would let a caller
    forget the pin.
    """

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    task_id: str
    run_id: str
    run_step_id: str | None
    workspace_id: str
    agent_id: str | None

    role: AgentRole
    instructions: str

    worktree: WorktreeLocation
    base_sha: str

    timeout_s: float = 1800
    max_turns: int = 40
    #: The owner-registered repo test command (doc 09a D3), passed to the command
    #: policy so a runtime can allow exactly that one command without asking.
    extra_allow: tuple[tuple[str, ...], ...] = ()
    output_schema: dict[str, Any] | None = None
    #: The model this task runs on (doc 19b). ``None`` uses the runtime's own
    #: default model, which is what every task got before agent modes.
    model: str | None = None


class RuntimeUsage(BaseModel):
    """Token and cost totals for one task, summed by the runtime.

    ``cache_tokens`` is the combined cache read + cache creation count. Gateway
    accounting keeps the two separate; the runtime seam only needs the total for
    the UI and the run cost.
    """

    model_config = ConfigDict(frozen=True)

    input_tokens: int = 0
    output_tokens: int = 0
    cache_tokens: int = 0
    cost_usd: Decimal = Decimal(0)


class AgentResult(BaseModel):
    """What one runtime call produced.

    ``error`` must be short and already redacted; it is not a transcript. The
    full transcript, when a runtime keeps one, is referenced by
    ``transcript_ref``.
    """

    model_config = ConfigDict(frozen=True)

    status: RunStatus
    summary: str
    structured_output: dict[str, Any] | None
    files_changed: tuple[str, ...]
    usage: RuntimeUsage
    error: str | None
    transcript_ref: str | None


class AgentRuntime(Protocol):
    """The seam between the orchestrator and one agent harness (ARCHITECTURE §3)."""

    name: str

    async def run_task(self, task: AgentTask, events: EventSink) -> AgentResult:
        """Run one task in ``task.worktree`` and return its result."""
        ...

    async def cancel(self, task_id: str) -> None:
        """Stop ``task_id`` as soon as possible. Must be safe to call twice."""
        ...


class UnknownRuntime(KeyError):
    """The registry does not know a runtime name."""

    def __init__(self, name: str) -> None:
        self.name = name
        super().__init__(name)


class RuntimeRegistry:
    """A per-process lookup of runtimes by name."""

    def __init__(self) -> None:
        self._runtimes: dict[str, AgentRuntime] = {}

    def register(self, runtime: AgentRuntime) -> None:
        """Register ``runtime`` under its ``name``.

        Duplicate names are refused: two different runtimes must not silently
        shadow each other in a process that runs several workspaces.
        """
        if runtime.name in self._runtimes:
            raise ValueError(f"runtime {runtime.name!r} is already registered")
        self._runtimes[runtime.name] = runtime

    def get(self, name: str) -> AgentRuntime:
        """Return the runtime registered as ``name``."""
        try:
            return self._runtimes[name]
        except KeyError:
            raise UnknownRuntime(name) from None

    def names(self) -> tuple[str, ...]:
        """The registered runtime names, sorted for deterministic output."""
        return tuple(sorted(self._runtimes))
