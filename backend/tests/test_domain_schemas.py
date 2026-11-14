"""Read and create schemas (doc 03).

A read schema is where a bad value in a text column becomes a validation error,
and a create schema is what stops an API caller from setting a status directly.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from gaia.db.models import Approval, Event, LlmUsage
from gaia.db.repo import EventRepo, ScopedRepo
from gaia.domain.enums import (
    ApprovalKind,
    ApprovalStatus,
    CiStatus,
    RunStatus,
    TicketStatus,
)
from gaia.domain.schemas import (
    ApprovalCreate,
    ApprovalRead,
    EventRead,
    LlmUsageCreate,
    LlmUsageRead,
    RepositoryCreate,
    RepositoryRead,
    RunRead,
    TicketCreate,
    TicketRead,
    WorkspaceRead,
)
from tests.db_builders import make_chain


async def test_a_ticket_row_validates_into_a_read_schema(session: AsyncSession) -> None:
    """``from_attributes`` reads a row, and the text status becomes an enum."""
    chain = await make_chain(session, slug="schema")
    read = TicketRead.model_validate(chain.ticket)
    assert read.status is TicketStatus.BACKLOG
    assert read.workspace_id == chain.workspace.id
    assert read.created_at.tzinfo is not None


async def test_a_bad_status_in_the_row_is_a_validation_error(session: AsyncSession) -> None:
    """The text column has no database enum, so the schema is the guard."""
    chain = await make_chain(session, slug="bad-status")
    chain.ticket.status = "not-a-status"
    with pytest.raises(ValidationError, match="status"):
        TicketRead.model_validate(chain.ticket)


async def test_a_setting_row_validates_into_its_schemas(session: AsyncSession) -> None:
    """The other read schemas read a row the same way."""
    chain = await make_chain(session, slug="second")
    workspace = WorkspaceRead.model_validate(chain.workspace)
    repository = RepositoryRead.model_validate(chain.repository)
    run = RunRead.model_validate(chain.run)
    assert workspace.slug == "second"
    assert repository.setup_command == ["uv", "sync", "--locked"]
    assert run.status is RunStatus.PENDING
    assert isinstance(run.cost_usd, Decimal)


async def test_an_event_read_keeps_its_seq(session: AsyncSession) -> None:
    """``seq`` is the SSE event id, so it must survive validation."""
    chain = await make_chain(session, slug="events")
    events = EventRepo(session, Event, chain.workspace.id)
    event = await events.append(type="run.started", run_id=chain.run.id)
    read = EventRead.model_validate(event)
    assert read.seq == event.seq
    assert read.type == "run.started"


async def test_llm_usage_create_and_read_agree(session: AsyncSession) -> None:
    """A create schema fills the defaults the column declares."""
    chain = await make_chain(session, slug="usage")
    create = LlmUsageCreate(run_id=chain.run.id, provider="anthropic", model="claude-opus-5-5")
    assert create.input_tokens == 0
    assert create.cost_usd == Decimal(0)

    usage_repo = ScopedRepo(session, LlmUsage, chain.workspace.id)
    row = await usage_repo.create(**create.model_dump())
    read = LlmUsageRead.model_validate(row)
    assert read.provider == "anthropic"
    assert read.cost_usd == Decimal(0)


async def test_an_approval_read_schema_validates_a_pending_row(session: AsyncSession) -> None:
    """A request that has not been answered yet has no decision fields."""
    chain = await make_chain(session, slug="approvals")
    approvals = ScopedRepo(session, Approval, chain.workspace.id)
    row = await approvals.create(
        run_id=chain.run.id,
        idempotency_key="command:tool-1",
        kind=ApprovalKind.COMMAND.value,
        payload_json={"command": "uv run pytest"},
    )
    read = ApprovalRead.model_validate(row)
    assert read.kind is ApprovalKind.COMMAND
    assert read.status is ApprovalStatus.PENDING
    assert read.decided_by is None
    assert read.decided_at is None


def test_a_ticket_create_cannot_set_a_status() -> None:
    """A ticket starts in ``backlog``; later moves go through the machine."""
    assert "status" not in TicketCreate.model_fields
    create = TicketCreate(repo_id="repo", title="A title")
    assert create.body == ""
    assert create.priority == 0
    assert create.external_ref is None


def test_a_repository_create_defaults_the_name_and_commands() -> None:
    """Only a path is required; the route fills in the name from the repo root."""
    create = RepositoryCreate(local_path="/src/gaia")
    assert create.name is None
    assert create.setup_command is None
    assert create.test_command is None


def test_repository_commands_are_argv_lists_with_limits() -> None:
    """Commands are lists of 1-64 non-empty items, each at most 1000 chars."""
    create = RepositoryCreate(
        name="gaia",
        local_path="/src/gaia",
        setup_command=["uv", "sync", "--locked"],
        test_command=["uv", "run", "pytest"],
    )
    assert create.setup_command == ["uv", "sync", "--locked"]
    assert create.test_command == ["uv", "run", "pytest"]

    with pytest.raises(ValidationError, match="between 1 and 64"):
        RepositoryCreate(name="gaia", local_path="/src/gaia", setup_command=[])

    with pytest.raises(ValidationError, match="1 and 1000"):
        RepositoryCreate(name="gaia", local_path="/src/gaia", setup_command=[""])

    with pytest.raises(ValidationError, match="NUL"):
        RepositoryCreate(
            name="gaia",
            local_path="/src/gaia",
            setup_command=["bad\x00argv"],
        )


def test_repository_commands_can_be_explicitly_none() -> None:
    """Pydantic runs the validator when a command is explicitly ``None``."""
    create = RepositoryCreate(
        name="gaia",
        local_path="/src/gaia",
        setup_command=None,
        test_command=None,
    )
    assert create.setup_command is None
    assert create.test_command is None


def test_an_approval_create_gets_its_own_payload_dict() -> None:
    """A mutable default is per instance, so two requests never share one."""
    first = ApprovalCreate(run_id="run", idempotency_key="plan:0:0", kind=ApprovalKind.PLAN)
    second = ApprovalCreate(run_id="run", idempotency_key="pr:0:0", kind=ApprovalKind.PR_OPEN)
    first.payload_json["steps"] = 3
    assert second.payload_json == {}


def test_an_approval_kind_must_be_known() -> None:
    """A payload from the UI is validated against the enum, not trusted."""
    with pytest.raises(ValidationError, match="kind"):
        ApprovalCreate.model_validate(
            {"run_id": "run", "idempotency_key": "plan:0:0", "kind": "not-a-kind"}
        )


def test_a_ci_status_round_trips_through_the_enum() -> None:
    """Read schemas coerce plain strings coming back from the database."""
    assert CiStatus("passed") is CiStatus.PASSED
