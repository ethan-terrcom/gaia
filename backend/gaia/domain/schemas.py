"""Pydantic read and create schemas for the domain (doc 03).

Owned by doc 03; docs 04 and 06 turn these into API responses and request
bodies. Three rules keep the boundary safe:

- **Read schemas say what a client may see**, and never a column the API should
  not expose: no key material exists in these tables, and none is added here.
- **Create schemas cannot set a status.** A ticket starts in ``backlog`` and a
  run in ``pending``; every later move goes through
  :func:`gaia.domain.state.transition`, so the API cannot skip the machine.
- **Enums are validated here.** The columns are text (see
  :mod:`gaia.db.models`), so a read schema is the place where a bad value in the
  database becomes a visible validation error rather than a mystery downstream.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from gaia.domain.enums import (
    AgentRole,
    AgentStatus,
    ApprovalKind,
    ApprovalStatus,
    CiStatus,
    PullRequestState,
    RunStatus,
    RunStepStatus,
    RuntimeKind,
    TicketStatus,
    WorktreeStatus,
)


class Schema(BaseModel):
    """Base for every schema: buildable straight from a SQLAlchemy row."""

    model_config = ConfigDict(from_attributes=True)


class RequestSchema(Schema):
    """Base for request bodies: no unknown fields may ride along."""

    model_config = ConfigDict(extra="forbid")


class ReadSchema(Schema):
    """Columns every table has."""

    id: str
    created_at: datetime
    updated_at: datetime


class WorkspaceScopedRead(ReadSchema):
    """A read schema for a tenant row."""

    workspace_id: str


class WorkspaceRead(Schema):
    """The tenant. Exposed as-is: it is the root of every query a client makes."""

    id: str
    name: str
    slug: str
    created_at: datetime
    updated_at: datetime


class UserRead(WorkspaceScopedRead):
    """The local user. No password, no token: auth is doc 04's bearer token."""

    email: str
    display_name: str


class RepositoryCreate(RequestSchema):
    """Register a repository.

    ``name`` is optional: the route defaults it to the repository root's
    directory name. The commands are argv lists (never a shell string), and they
    are the owner's registration-time values, never read from the repository.
    """

    name: str | None = Field(default=None, min_length=1, max_length=200)
    local_path: str = Field(min_length=1, max_length=4096)
    setup_command: list[str] | None = None
    test_command: list[str] | None = None

    @field_validator("setup_command", "test_command")
    @classmethod
    def _validate_argv(cls, value: list[str] | None) -> list[str] | None:
        """Commands are argv lists: 1-64 items, each 1-1000 chars, no NUL."""
        if value is None:
            return value
        if not 1 <= len(value) <= 64:
            raise ValueError("an argv list must contain between 1 and 64 items")
        for item in value:
            if len(item) < 1 or len(item) > 1000:
                raise ValueError("each argv item must be between 1 and 1000 characters")
            if "\x00" in item:
                raise ValueError("argv items must not contain NUL characters")
        return value


class RepositoryRead(WorkspaceScopedRead):
    """A registered repository."""

    name: str
    local_path: str
    remote_url: str | None
    default_branch: str
    setup_command: list[str] | None
    test_command: list[str] | None
    created_by: str | None


class RepositoryDetailRead(RepositoryRead):
    """A repository with the two counts the dashboard shows."""

    worktree_count: int
    active_runs: int


class TicketCreate(RequestSchema):
    """Open a ticket. ``status`` is deliberately absent (see the module docstring)."""

    repo_id: str
    title: str = Field(min_length=1, max_length=200)
    body: str = Field(default="", max_length=50_000)
    priority: int = 0
    external_ref: str | None = None


class TicketPatch(RequestSchema):
    """Update a ticket. ``status`` goes through the transition machine."""

    title: str | None = Field(default=None, min_length=1, max_length=200)
    body: str | None = Field(default=None, max_length=50_000)
    priority: int | None = None
    status: TicketStatus | None = None

    @model_validator(mode="after")
    def _at_least_one_field(self) -> TicketPatch:
        """A PATCH with no fields is a client bug, not a write."""
        if all(value is None for value in (self.title, self.body, self.priority, self.status)):
            raise ValueError("at least one field must be provided")
        return self


class TicketRead(WorkspaceScopedRead):
    """A ticket, with its status validated against :class:`TicketStatus`."""

    repo_id: str
    title: str
    body: str
    status: TicketStatus
    priority: int
    external_ref: str | None
    created_by: str | None


class TicketPage(Schema):
    """One page of tickets and the cursor for the next one."""

    items: list[TicketRead]
    next_cursor: str | None


class AgentRead(WorkspaceScopedRead):
    """A configured agent."""

    role: AgentRole
    runtime: RuntimeKind
    model: str
    display_name: str
    status: AgentStatus


class WorktreeRead(WorkspaceScopedRead):
    """A git worktree (doc 05)."""

    repo_id: str
    path: str
    branch: str
    base_sha: str | None
    status: WorktreeStatus


class WorktreeItemRead(WorktreeRead):
    """A worktree as the list endpoint shows it.

    ``dirty`` is ``None`` when a tampered worktree could not be inspected safely;
    ``tampered`` is then ``True`` instead of failing the whole list.
    """

    orphan: bool
    exists: bool
    dirty: bool | None
    tampered: bool


class WorktreeListRead(Schema):
    """The worktree list: rows plus paths git knows that no row claims."""

    items: list[WorktreeItemRead]
    unknown_paths: list[str]


class RunRead(WorkspaceScopedRead):
    """A run, including the money that has been spent on it so far."""

    ticket_id: str
    repo_id: str
    worktree_id: str | None
    junior_runtime: RuntimeKind | None
    status: RunStatus
    graph_thread_id: str | None
    cost_usd: Decimal
    budget_usd: Decimal
    started_at: datetime | None
    finished_at: datetime | None
    created_by: str | None


class RunStepRead(WorkspaceScopedRead):
    """One step of a run's plan."""

    run_id: str
    step_index: int
    title: str
    spec_json: dict[str, Any]
    status: RunStepStatus
    assigned_agent_id: str | None
    attempts: int
    result_summary: str | None


class ApprovalCreate(Schema):
    """Ask the human something. The decision is a separate call (doc 14).

    ``idempotency_key`` is what makes a resumed node upsert the same request
    rather than insert a duplicate inbox entry (doc 12): graph gates use
    ``"<node>:<step_index>:<attempt>"`` and in-turn command approvals use
    ``"command:<tool_call_id>"``.
    """

    run_id: str
    idempotency_key: str
    kind: ApprovalKind
    payload_json: dict[str, Any] = Field(default_factory=dict)


class ApprovalRead(WorkspaceScopedRead):
    """An approval request and, once decided, its outcome."""

    run_id: str
    idempotency_key: str
    kind: ApprovalKind
    payload_json: dict[str, Any]
    status: ApprovalStatus
    decided_by: str | None
    decided_at: datetime | None
    comment: str | None


class PullRequestRead(WorkspaceScopedRead):
    """The PR a run opened, and GitHub's view of it (doc 15)."""

    run_id: str
    number: int
    url: str
    state: PullRequestState
    ci_status: CiStatus


class EventRead(WorkspaceScopedRead):
    """One event. ``seq`` is the SSE event id a client resumes from (doc 13)."""

    seq: int
    run_id: str | None
    agent_id: str | None
    type: str
    payload_json: dict[str, Any]
    ts: datetime


class LlmUsageCreate(Schema):
    """Record one LLM call (doc 07 writes these)."""

    run_id: str
    run_step_id: str | None = None
    agent_id: str | None = None
    provider: str
    model: str
    request_id: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0
    price_tier: str | None = None
    cost_usd: Decimal = Decimal(0)


class LlmUsageRead(WorkspaceScopedRead):
    """One LLM call's token counts and cost."""

    run_id: str
    run_step_id: str | None
    agent_id: str | None
    provider: str
    model: str
    request_id: str | None
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_creation_tokens: int
    price_tier: str | None
    cost_usd: Decimal


class AuditLogRead(WorkspaceScopedRead):
    """One line of the audit trail (doc 09b writes these)."""

    actor: str
    action: str
    target: str | None
    details_json: dict[str, Any]
    ts: datetime
