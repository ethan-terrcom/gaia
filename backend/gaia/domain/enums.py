"""Domain enumerations shared by the models, the schemas and the state machine.

Owned by doc 03. Values are stored as text (``VARCHAR``) rather than as a
dialect enum type, so a migration never has to alter a PostgreSQL type and the
schema stays dialect-neutral (ARCHITECTURE section 7). The type safety lives at
the two places where rows are written: the Pydantic schemas validate the values,
and :func:`gaia.domain.state.transition` refuses an illegal move.

Doc 03 names ``TicketStatus``, ``RunStatus``, ``ApprovalKind``,
``ApprovalStatus``, ``AgentRole`` and ``RuntimeKind``. The others below back
columns that ARCHITECTURE section 5 names without listing their values
(``agent.status``, ``run_step.status``, ``worktree.status``,
``pull_request.state`` and ``ci_status``); they are added here so no service
invents its own strings.
"""

from __future__ import annotations

from enum import StrEnum


class AgentRole(StrEnum):
    """Who an agent is in the hierarchy: the lead plans and reviews, juniors implement."""

    LEAD = "lead"
    JUNIOR = "junior"


class AgentStatus(StrEnum):
    """Whether an agent row is usable and busy."""

    IDLE = "idle"
    RUNNING = "running"
    PAUSED = "paused"
    FAILED = "failed"
    RETIRED = "retired"


class RuntimeKind(StrEnum):
    """The harness behind an agent; ADR-001 (doc 08) picks which ones ship.

    ``native_claude`` and ``native_deepseek`` are the raw-API runtimes,
    ``claude_agent_sdk`` and ``cline_cli`` the harnesses. The values are stored
    on ``agent.runtime`` and ``run.junior_runtime``, so they must not change
    after the first migration without one.
    """

    NATIVE_CLAUDE = "native_claude"
    NATIVE_DEEPSEEK = "native_deepseek"
    CLAUDE_AGENT_SDK = "claude_agent_sdk"
    CLINE_CLI = "cline_cli"


class TicketStatus(StrEnum):
    """Where a ticket is in the queue (doc 03 task 3)."""

    BACKLOG = "backlog"
    READY = "ready"
    IN_PROGRESS = "in_progress"
    IN_REVIEW = "in_review"
    DONE = "done"
    CANCELLED = "cancelled"


class RunStatus(StrEnum):
    """Where a run is in the graph of ARCHITECTURE section 4.

    ``awaiting_approval`` covers every human gate between agent turns, and
    ``paused_budget`` the budget interrupt. ``failed`` and ``completed`` are
    terminal; a failed run is resumed from its checkpoint instead of being
    revived by a status change.
    """

    PENDING = "pending"
    PLANNING = "planning"
    AWAITING_APPROVAL = "awaiting_approval"
    IMPLEMENTING = "implementing"
    REVIEWING = "reviewing"
    PR_OPEN = "pr_open"
    PAUSED_BUDGET = "paused_budget"
    FAILED = "failed"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


class RunStepStatus(StrEnum):
    """Where a single step of the plan is (``run_step.status``)."""

    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class WorktreeStatus(StrEnum):
    """Lifecycle of a git worktree (doc 05 owns the transitions)."""

    PREPARING = "preparing"
    READY = "ready"
    IN_USE = "in_use"
    REMOVED = "removed"
    FAILED = "failed"


class ApprovalKind(StrEnum):
    """What a human is being asked to approve (doc 03 task 3).

    ``plan``, ``escalation``, ``budget``, ``pr_open`` and ``merge`` are graph
    interrupts; ``command`` is asked *inside* an agent turn by the runtime's
    permission hook (2026-11-12 review, section 3.1).
    """

    PLAN = "plan"
    COMMAND = "command"
    ESCALATION = "escalation"
    PR_OPEN = "pr_open"
    MERGE = "merge"
    BUDGET = "budget"


class ApprovalStatus(StrEnum):
    """The outcome of an approval request; ``expired`` is the default-deny timeout."""

    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"


class PullRequestState(StrEnum):
    """The part of a GitHub PR's state Gaia cares about (doc 15)."""

    OPEN = "open"
    MERGED = "merged"
    CLOSED = "closed"


class CiStatus(StrEnum):
    """The part of a GitHub checks rollup Gaia cares about (doc 15)."""

    UNKNOWN = "unknown"
    PENDING = "pending"
    PASSED = "passed"
    FAILED = "failed"


class EventType(StrEnum):
    """Every event the bus can carry (doc 13).

    ``stream.gap`` is transport-only: the bus inserts it into a subscriber's
    queue when that subscriber overflowed, but it is never written to the
    ``event`` table.
    """

    RUN_CREATED = "run.created"
    RUN_STATUS_CHANGED = "run.status_changed"
    RUN_NODE_STARTED = "run.node_started"
    RUN_NODE_FINISHED = "run.node_finished"
    RUN_BUDGET_EXCEEDED = "run.budget_exceeded"
    RUN_FAILED = "run.failed"

    STEP_STARTED = "step.started"
    STEP_FINISHED = "step.finished"

    AGENT_MESSAGE = "agent.message"
    AGENT_TOOL_CALL = "agent.tool_call"
    AGENT_TOOL_RESULT = "agent.tool_result"
    AGENT_USAGE = "agent.usage"

    APPROVAL_REQUESTED = "approval.requested"
    APPROVAL_DECIDED = "approval.decided"

    PR_OPENED = "pr.opened"
    PR_UPDATED = "pr.updated"
    PR_MERGED = "pr.merged"
    CI_STATUS = "ci.status"

    WORKTREE_CREATED = "worktree.created"
    WORKTREE_REMOVED = "worktree.removed"

    STREAM_GAP = "stream.gap"
