"""The SQLAlchemy 2.x models for ARCHITECTURE section 5.

Owned by doc 03. Three conventions run through this file:

- **Every tenant row carries ``workspace_id``** and is only ever reached through
  :class:`gaia.db.repo.ScopedRepo`, which filters on it (doc 03 task 5).
- **Enums are stored as text.** ``String(32)`` columns hold the ``.value`` of a
  :mod:`gaia.domain.enums` member. A dialect enum type would need a migration to
  add a value, and SQLite has none at all (ARCHITECTURE section 7 asks for
  dialect-neutral migrations). The Pydantic schemas validate on write and
  :func:`gaia.domain.state.transition` validates moves.
- **Timestamps are aware UTC** through :class:`gaia.db.types.UtcDateTime`, and
  money is ``Numeric(12, 6)`` - ``Decimal``, never ``float``.

Two deliberate departures from a literal reading of section 5:

- ``workspace`` has no ``workspace_id``: it *is* the tenant.
- ``run_step.step_index`` is named that way rather than ``index``, which is a
  reserved word in both SQLite and PostgreSQL.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    JSON,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from gaia.db.ids import uuid7
from gaia.db.types import UtcDateTime, utcnow
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

#: Deterministic constraint names, so Alembic sees the same names on every run
#: and a later migration can drop what an earlier one created.
NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

#: Column widths. ``Text`` where the value has no sensible cap.
NAME = 200
PATH = 4096
URL = 2048


class Base(DeclarativeBase):
    """Declarative base with the project's naming convention."""

    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class UUIDPrimaryKey:
    """``id``: a UUIDv7 string, generated in Python so SQLite and Postgres agree."""

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid7)


class Timestamps:
    """``created_at`` and ``updated_at``, both aware UTC."""

    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow, onupdate=utcnow)


class WorkspaceScoped:
    """``workspace_id``: the tenant every row belongs to and is filtered by."""

    workspace_id: Mapped[str] = mapped_column(
        # Deleting a workspace deletes its data; that is the tenant boundary.
        ForeignKey("workspace.id", ondelete="CASCADE"),
        index=True,
    )


class CreatedBy:
    """``created_by``: the local user that asked for the row, when known."""

    created_by: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), default=None
    )


class Workspace(Base):
    """The tenant. Single-user today, but every other row points at one.

    ``slug`` is the stable, human-typable handle; ``name`` is for display.
    """

    __tablename__ = "workspace"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid7)
    name: Mapped[str] = mapped_column(String(NAME))
    slug: Mapped[str] = mapped_column(String(64), unique=True)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow, onupdate=utcnow)


class User(UUIDPrimaryKey, WorkspaceScoped, Timestamps, Base):
    """A person. The table is ``users`` because ``user`` is reserved in PostgreSQL."""

    __tablename__ = "users"
    __table_args__ = (UniqueConstraint("workspace_id", "email"),)

    email: Mapped[str] = mapped_column(String(320))
    display_name: Mapped[str] = mapped_column(String(NAME))


class Repository(UUIDPrimaryKey, WorkspaceScoped, CreatedBy, Timestamps, Base):
    """A git repository Gaia may work in.

    ``setup_command`` and ``test_command`` are registered by the owner and never
    read from the repo: a checked-out branch is untrusted content (the
    2026-11-12 review, section 5.1).
    """

    __tablename__ = "repository"
    __table_args__ = (UniqueConstraint("workspace_id", "name"),)

    name: Mapped[str] = mapped_column(String(NAME))
    local_path: Mapped[str] = mapped_column(String(PATH))
    remote_url: Mapped[str | None] = mapped_column(String(URL), default=None)
    default_branch: Mapped[str] = mapped_column(String(255), default="main")
    setup_command: Mapped[list[str] | None] = mapped_column(JSON, default=None)
    test_command: Mapped[list[str] | None] = mapped_column(JSON, default=None)


class Ticket(UUIDPrimaryKey, WorkspaceScoped, CreatedBy, Timestamps, Base):
    """A unit of work for one repository."""

    __tablename__ = "ticket"
    __table_args__ = (
        UniqueConstraint("repo_id", "external_ref"),
        # Doc 03 task 2: the (workspace_id, status) index the board view needs.
        Index("ix_ticket_workspace_id_status", "workspace_id", "status"),
    )

    repo_id: Mapped[str] = mapped_column(
        ForeignKey("repository.id", ondelete="CASCADE"), index=True
    )
    title: Mapped[str] = mapped_column(String(500))
    body: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(32), default=TicketStatus.BACKLOG.value)
    priority: Mapped[int] = mapped_column(Integer, default=0)
    #: Set when the ticket was imported from an issue tracker (doc 15c).
    external_ref: Mapped[str | None] = mapped_column(String(255), default=None)


class Agent(UUIDPrimaryKey, WorkspaceScoped, Timestamps, Base):
    """A configured agent: a role, a runtime and the model it drives."""

    __tablename__ = "agent"

    role: Mapped[str] = mapped_column(String(16), default=AgentRole.JUNIOR.value)
    runtime: Mapped[str] = mapped_column(String(32), default=RuntimeKind.NATIVE_DEEPSEEK.value)
    model: Mapped[str] = mapped_column(String(NAME))
    display_name: Mapped[str] = mapped_column(String(NAME))
    status: Mapped[str] = mapped_column(String(32), default=AgentStatus.IDLE.value)


class Worktree(UUIDPrimaryKey, WorkspaceScoped, Timestamps, Base):
    """A git worktree created for a repository (doc 05 owns its lifecycle)."""

    __tablename__ = "worktree"
    __table_args__ = (UniqueConstraint("repo_id", "branch"),)

    repo_id: Mapped[str] = mapped_column(
        ForeignKey("repository.id", ondelete="CASCADE"), index=True
    )
    path: Mapped[str] = mapped_column(String(PATH))
    branch: Mapped[str] = mapped_column(String(255))
    #: The commit the worktree started from, so a diff can be taken from it.
    base_sha: Mapped[str | None] = mapped_column(String(64), default=None)
    #: The trusted admin dir (``<repo>/.git/worktrees/<id>``) recorded at creation.
    #: Git commands run in the worktree with ``GIT_DIR`` set to this value, never
    #: through the worktree's own rewritable ``.git`` file (review finding F1).
    git_dir: Mapped[str | None] = mapped_column(String(PATH), default=None)
    status: Mapped[str] = mapped_column(String(32), default=WorktreeStatus.PREPARING.value)


class Run(UUIDPrimaryKey, WorkspaceScoped, CreatedBy, Timestamps, Base):
    """One pass of the graph in ARCHITECTURE section 4 over one ticket."""

    __tablename__ = "run"
    __table_args__ = (Index("ix_run_workspace_id_status", "workspace_id", "status"),)

    ticket_id: Mapped[str] = mapped_column(ForeignKey("ticket.id", ondelete="CASCADE"), index=True)
    repo_id: Mapped[str] = mapped_column(
        ForeignKey("repository.id", ondelete="CASCADE"), index=True
    )
    worktree_id: Mapped[str | None] = mapped_column(
        ForeignKey("worktree.id", ondelete="SET NULL"), default=None
    )
    #: Chosen at implementation time, so it can still be unset while planning.
    junior_runtime: Mapped[str | None] = mapped_column(String(32), default=None)
    status: Mapped[str] = mapped_column(String(32), default=RunStatus.PENDING.value)
    #: LangGraph's ``thread_id``. The graph owns the workflow position; the run
    #: row is a projection that nodes upsert (the 2026-11-12 review, section 3.2).
    graph_thread_id: Mapped[str | None] = mapped_column(String(64), default=None)
    cost_usd: Mapped[Decimal] = mapped_column(Numeric(12, 6), default=Decimal(0))
    budget_usd: Mapped[Decimal] = mapped_column(Numeric(12, 6), default=Decimal(0))
    started_at: Mapped[datetime | None] = mapped_column(UtcDateTime, default=None)
    finished_at: Mapped[datetime | None] = mapped_column(UtcDateTime, default=None)


class RunStep(UUIDPrimaryKey, WorkspaceScoped, Timestamps, Base):
    """One plan step inside a run.

    ``spec_json`` holds the ``PlanStep`` the lead produced, so a resumed run does
    not have to re-ask the model what it had in mind.
    """

    __tablename__ = "run_step"
    __table_args__ = (UniqueConstraint("run_id", "step_index"),)

    run_id: Mapped[str] = mapped_column(ForeignKey("run.id", ondelete="CASCADE"), index=True)
    step_index: Mapped[int] = mapped_column(Integer)
    title: Mapped[str] = mapped_column(String(500))
    spec_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(32), default=RunStepStatus.PENDING.value)
    assigned_agent_id: Mapped[str | None] = mapped_column(
        ForeignKey("agent.id", ondelete="SET NULL"), default=None
    )
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    result_summary: Mapped[str | None] = mapped_column(Text, default=None)


class Approval(UUIDPrimaryKey, WorkspaceScoped, Timestamps, Base):
    """A question for the human. Every irreversible step waits on one.

    ``idempotency_key`` makes a resumed node upsert the same approval instead of
    inserting a duplicate request. Graph gates use ``"<node>:<step_index>:<attempt>"``;
    in-turn command approvals use ``"command:<tool_call_id>"``. The unique
    constraint is ``(run_id, idempotency_key)`` so the same key on a different
    run is a fresh request.
    """

    __tablename__ = "approval"
    __table_args__ = (UniqueConstraint("run_id", "idempotency_key"),)

    run_id: Mapped[str] = mapped_column(ForeignKey("run.id", ondelete="CASCADE"), index=True)
    idempotency_key: Mapped[str] = mapped_column(String(200))
    kind: Mapped[str] = mapped_column(String(16), default=ApprovalKind.PLAN.value)
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(16), default=ApprovalStatus.PENDING.value)
    decided_by: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), default=None
    )
    decided_at: Mapped[datetime | None] = mapped_column(UtcDateTime, default=None)
    comment: Mapped[str | None] = mapped_column(Text, default=None)


class PullRequest(UUIDPrimaryKey, WorkspaceScoped, Timestamps, Base):
    """The PR a run opened, and what GitHub says about it (doc 15)."""

    __tablename__ = "pull_request"
    __table_args__ = (UniqueConstraint("run_id", "number"),)

    run_id: Mapped[str] = mapped_column(ForeignKey("run.id", ondelete="CASCADE"), index=True)
    number: Mapped[int] = mapped_column(Integer)
    url: Mapped[str] = mapped_column(String(URL))
    state: Mapped[str] = mapped_column(String(16), default=PullRequestState.OPEN.value)
    ci_status: Mapped[str] = mapped_column(String(16), default=CiStatus.UNKNOWN.value)


class Event(Base):
    """An append-only event for the UI, and the SSE replay log (doc 13).

    ``seq`` is the primary key, not ``id``: SQLite can only autoincrement an
    ``INTEGER PRIMARY KEY``, and ``seq`` must be a database-assigned monotonic
    integer so ``Last-Event-ID`` replay has a cursor that UUIDv7 cannot give
    (the 2026-11-12 review, section 4). ``id`` stays unique, so a client that
    sees an event twice can still deduplicate on it.
    """

    __tablename__ = "event"
    __table_args__ = (
        Index("ix_event_run_id_seq", "run_id", "seq"),
        UniqueConstraint("id"),
    )

    seq: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    id: Mapped[str] = mapped_column(String(36), default=uuid7)
    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspace.id", ondelete="CASCADE"), index=True
    )
    #: SET NULL, not CASCADE: the trail of a deleted run is still history.
    run_id: Mapped[str | None] = mapped_column(
        ForeignKey("run.id", ondelete="SET NULL"), default=None
    )
    agent_id: Mapped[str | None] = mapped_column(
        ForeignKey("agent.id", ondelete="SET NULL"), default=None
    )
    type: Mapped[str] = mapped_column(String(100))
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    ts: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow, onupdate=utcnow)


class LlmUsage(UUIDPrimaryKey, WorkspaceScoped, Timestamps, Base):
    """One LLM call's token counts and cost (doc 07 writes these rows)."""

    __tablename__ = "llm_usage"

    run_id: Mapped[str] = mapped_column(ForeignKey("run.id", ondelete="CASCADE"), index=True)
    run_step_id: Mapped[str | None] = mapped_column(
        ForeignKey("run_step.id", ondelete="SET NULL"), default=None
    )
    agent_id: Mapped[str | None] = mapped_column(
        ForeignKey("agent.id", ondelete="SET NULL"), default=None
    )
    provider: Mapped[str] = mapped_column(String(32))
    model: Mapped[str] = mapped_column(String(NAME))
    #: The provider's request id, for reconciling a bill with a trace.
    request_id: Mapped[str | None] = mapped_column(String(128), default=None, index=True)
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cache_read_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cache_creation_tokens: Mapped[int] = mapped_column(Integer, default=0)
    #: The pricing band that was applied, for example DeepSeek's peak/off-peak.
    price_tier: Mapped[str | None] = mapped_column(String(32), default=None)
    cost_usd: Mapped[Decimal] = mapped_column(Numeric(12, 6), default=Decimal(0))


class AuditLog(UUIDPrimaryKey, WorkspaceScoped, Timestamps, Base):
    """Append-only record of every tool call, command, decision and push.

    ``actor`` is free text ("user:<id>", "agent:<name>", "system"): the actor of
    a tool call may be an agent, which has no ``users`` row.
    """

    __tablename__ = "audit_log"

    actor: Mapped[str] = mapped_column(String(NAME))
    action: Mapped[str] = mapped_column(String(100))
    target: Mapped[str | None] = mapped_column(String(500), default=None)
    details_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    ts: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow)
