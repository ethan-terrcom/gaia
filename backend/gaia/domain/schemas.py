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

from datetime import date, datetime
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from gaia.domain.agent_modes import AgentModeConfig
from gaia.domain.enums import (
    AgentRole,
    AgentStatus,
    ApprovalKind,
    ApprovalStatus,
    CiStatus,
    LLMProvider,
    ModelTier,
    PullRequestState,
    RunStatus,
    RunStepStatus,
    RuntimeKind,
    TicketStatus,
    WorktreeStatus,
)
from gaia.security.commands import allowed_entry_problem


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


class SessionRead(Schema):
    """The authenticated caller plus process-wide flags the UI needs (18b-2)."""

    user_id: str
    workspace_id: str
    demo_mode: bool


def _check_argv(value: list[str] | None) -> list[str] | None:
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
        return _check_argv(value)


#: Most allowed commands one repository may register.
MAX_ALLOWED_COMMANDS = 100


class RepositoryUpdate(RequestSchema):
    """Change a registered repository's commands.

    Only the fields that are sent change; ``null`` clears a command. The path,
    name and remote are not editable: they identify the repository.
    ``allowed_commands`` replaces the whole list; each entry is an argv prefix
    an agent may run without asking.
    """

    setup_command: list[str] | None = None
    test_command: list[str] | None = None
    allowed_commands: list[list[str]] | None = None

    @field_validator("setup_command", "test_command")
    @classmethod
    def _validate_argv(cls, value: list[str] | None) -> list[str] | None:
        """Same argv rules as registration."""
        return _check_argv(value)

    @field_validator("allowed_commands")
    @classmethod
    def _validate_allowed(cls, value: list[list[str]] | None) -> list[list[str]] | None:
        """Each entry must pass the command policy's rules; duplicates are dropped."""
        if value is None:
            return value
        if len(value) > MAX_ALLOWED_COMMANDS:
            raise ValueError(f"at most {MAX_ALLOWED_COMMANDS} allowed commands")
        unique: list[list[str]] = []
        for entry in value:
            problem = allowed_entry_problem(entry)
            if problem is not None:
                raise ValueError(problem)
            if entry not in unique:
                unique.append(entry)
        return unique


class RepositoryRead(WorkspaceScopedRead):
    """A registered repository."""

    name: str
    local_path: str
    remote_url: str | None
    default_branch: str
    setup_command: list[str] | None
    test_command: list[str] | None
    allowed_commands: list[list[str]]
    created_by: str | None

    @field_validator("allowed_commands", mode="before")
    @classmethod
    def _none_is_empty(cls, value: list[list[str]] | None) -> list[list[str]]:
        """A repository that never registered any has an empty list."""
        return [] if value is None else value


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
    #: The agent mode and models the run started with; ``None`` for runs started
    #: before doc 19b, which use the settings-derived config.
    agent_config: AgentModeConfig | None = None


class RunCreate(RequestSchema):
    """Start a run. The status starts at ``pending`` and cannot be set here.

    ``agent_config`` overrides the saved default for this run (doc 19b, M3).
    ``junior_runtime`` is the older per-run override, kept for API clients; it
    cannot be combined with ``agent_config``.
    """

    ticket_id: str
    junior_runtime: RuntimeKind | None = None
    budget_usd: Decimal | None = Field(default=None, gt=0)
    agent_config: AgentModeConfig | None = None


class RunCurrentStep(Schema):
    """The first not-yet-completed step of a run, for the dashboard list."""

    index: int
    title: str


class RunSummary(RunRead):
    """One row of the dashboard runs list (doc 14)."""

    ticket_title: str
    current_step: RunCurrentStep | None
    pr_number: int | None
    pr_url: str | None
    ci_status: CiStatus | None


class RunPage(Schema):
    """One page of runs and the cursor for the next page."""

    items: list[RunSummary]
    next_cursor: str | None


class RunTicketSummary(Schema):
    """The ticket fields a run detail shows beside the run."""

    id: str
    title: str
    status: TicketStatus


class RunWorktreeSummary(Schema):
    """The worktree fields a run detail shows."""

    branch: str
    path: str
    status: WorktreeStatus


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


class ApprovalPage(Schema):
    """The approval inbox list, plus the workspace's pending count.

    ``total_pending`` ignores the list filters on purpose: the nav badge needs
    one call that still shows the real count when the page is filtered.
    """

    items: list[ApprovalRead]
    total_pending: int


class PullRequestRead(ReadSchema):
    """A PR as the pull-request list route shows it (doc 15b)."""

    number: int
    url: str
    state: PullRequestState
    ci_status: CiStatus
    review_state: str | None
    head_sha: str | None
    run_id: str
    run_status: RunStatus
    ticket_id: str
    ticket_title: str
    repo_name: str


class PullRequestPage(Schema):
    """One page of pull requests (doc 15b)."""

    items: list[PullRequestRead]


class PullRequestPendingApproval(Schema):
    """A pending gate on the PR's run that the PR desk can deep-link to."""

    id: str
    kind: ApprovalKind


class PullRequestDetail(PullRequestRead):
    """One PR for the in-app desk: list fields plus worktree and pending gates."""

    worktree_path: str | None = None
    worktree_branch: str | None = None
    pending_approvals: list[PullRequestPendingApproval] = Field(default_factory=list)


class RunPullRequestRead(ReadSchema):
    """The PR fields a run detail shows, without the list route's repo context."""

    number: int
    url: str
    state: PullRequestState
    ci_status: CiStatus
    review_state: str | None
    head_sha: str | None


class RunDetail(Schema):
    """Everything the run detail endpoint shows (doc 14)."""

    run: RunRead
    ticket: RunTicketSummary
    steps: list[RunStepRead]
    worktree: RunWorktreeSummary | None
    approvals: list[ApprovalRead]
    pull_request: RunPullRequestRead | None


class CommandSuggestion(Schema):
    """A command prefix suggested from the owner's past approvals in one repository."""

    argv: list[str]
    times_approved: int


class CommandSuggestionList(Schema):
    """Suggested allowed commands, most often approved first."""

    items: list[CommandSuggestion]


class DecideRequest(RequestSchema):
    """A human decision on one approval (doc 14).

    ``remember`` is valid only when approving a command: the command's prefix
    (``payload.remember_prefix``) is added to the repository's allowed commands
    as well.
    """

    decision: Literal["approve", "reject", "edit"]
    remember: bool = False
    comment: str | None = Field(default=None, max_length=4000)
    edited_payload: dict[str, Any] | None = None
    confirm: str | None = None
    budget_usd: Decimal | None = Field(default=None, gt=0)


class DecideAccepted(Schema):
    """The 202 acknowledgement for a graph-gate decision."""

    approval_id: str
    status: Literal["accepted"]


class PauseStatus(Schema):
    """Whether the global kill switch is on (doc 18a)."""

    paused: bool
    since: datetime | None
    reason: str | None


class PauseAllRequest(RequestSchema):
    """Reason for pausing every run. Global, not per workspace: Gaia is single-user."""

    reason: str | None = Field(default=None, max_length=500)


class PauseAllResult(Schema):
    """The ids paused by this call (usually empty when already paused)."""

    paused_run_ids: list[str]


class ResumeAllResult(Schema):
    """The ids that were ``paused`` and are now being relaunched."""

    resumed_run_ids: list[str]


class AutoModeStatus(Schema):
    """Which model families run in auto mode.

    In auto mode a command the policy would ask about runs without waiting for
    a human; denied commands, the sandbox and protected-file writes are unchanged.
    """

    claude: bool
    deepseek: bool
    openai: bool
    xai: bool


class AutoModeUpdate(RequestSchema):
    """Switch auto mode on or off per family. An omitted family is left as it is."""

    claude: bool | None = None
    deepseek: bool | None = None
    openai: bool | None = None
    xai: bool | None = None


class AgentModeDefault(Schema):
    """The agent mode a new run uses unless it names its own (doc 19b).

    ``saved`` is false when the owner has not saved a default yet, so the
    config shown is the one derived from settings (two tiered, today's models).
    """

    config: AgentModeConfig
    saved: bool


class ProviderStatus(Schema):
    """One LLM provider and whether its API key is in the keychain (19a)."""

    name: LLMProvider
    configured: bool
    #: The keychain entry to set with ``gaia secrets set``; a name, never a value.
    secret_name: str


class CatalogModelRead(Schema):
    """One model Gaia can put in a role, with its list prices (USD per 1M tokens)."""

    id: str
    provider: LLMProvider
    label: str
    tier: ModelTier | None
    #: Whether the provider's key is set, so a run can use this model now.
    available: bool
    input_usd_per_mtok: Decimal
    output_usd_per_mtok: Decimal
    cache_read_usd_per_mtok: Decimal


class ModelCatalog(Schema):
    """Every provider and every priced model (``GET /models``)."""

    providers: list[ProviderStatus]
    models: list[CatalogModelRead]


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


class ProviderCost(Schema):
    """Today's spend for one LLM provider."""

    provider: str
    cost_usd: Decimal


class TodayCosts(Schema):
    """The numbers for the top bar's daily spend meter (doc 16)."""

    spent_usd: Decimal
    budget_usd: Decimal
    by_provider: list[ProviderCost]
    day_start: datetime


class CostByModel(Schema):
    """LLM spend for one provider/model pair over a window (doc 18b)."""

    provider: str
    model: str
    cost_usd: Decimal


class CostByDay(Schema):
    """LLM spend for one UTC day over a window (doc 18b)."""

    day: date
    cost_usd: Decimal


class ApprovalWaitSummary(Schema):
    """Human wait time for approvals over a window (doc 18b)."""

    count: int
    avg: float
    p50: float
    p90: float


class MetricsSummary(Schema):
    """The observability dashboard's workspace summary (doc 18b)."""

    runs_by_status: dict[str, int]
    success_rate: float | None
    avg_cost_per_run_usd: Decimal | None
    cost_by_model: list[CostByModel]
    cost_by_day: list[CostByDay]
    approval_wait_s: ApprovalWaitSummary | None


class AuditLogRead(WorkspaceScopedRead):
    """One line of the audit trail (doc 09b writes these)."""

    actor: str
    action: str
    target: str | None
    details_json: dict[str, Any]
    ts: datetime
