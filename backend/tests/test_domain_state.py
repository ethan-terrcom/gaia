"""Status machines (doc 03 task 4): legal moves pass, illegal ones raise.

The rows here are never flushed. The machine only reads and writes ``status``, so
a bare model instance is enough to test it, and it keeps these tests about the
tables rather than about the database.
"""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from typing import cast

import pytest

from gaia.db.models import Approval, Run, RunStep, Ticket
from gaia.domain.enums import RunStatus, RunStepStatus, TicketStatus
from gaia.domain.state import (
    RUN_STEP_TRANSITIONS,
    RUN_TRANSITIONS,
    TICKET_TRANSITIONS,
    IllegalTransition,
    UnknownStatus,
    transition,
)


def test_a_legal_move_writes_the_status() -> None:
    """The row keeps the text value, so the column stays dialect-neutral."""
    ticket = Ticket(title="A ticket", status=TicketStatus.BACKLOG.value)
    transition(ticket, TicketStatus.READY)
    assert ticket.status == "ready"


def test_a_move_to_the_current_status_is_a_noop() -> None:
    """Nodes re-apply statuses when a run resumes, so this cannot raise."""
    run = Run(status=RunStatus.IMPLEMENTING.value)
    transition(run, RunStatus.IMPLEMENTING)
    assert run.status == "implementing"


def test_the_status_can_be_given_as_a_plain_string() -> None:
    """Callers that read a status from a payload do not need the enum."""
    ticket = Ticket(title="A ticket", status=TicketStatus.READY.value)
    transition(ticket, "in_progress")
    assert ticket.status == "in_progress"


def test_an_illegal_move_raises_and_names_the_alternatives() -> None:
    """``backlog`` cannot jump to ``done``; the message says what it can do."""
    ticket = Ticket(title="A ticket", status=TicketStatus.BACKLOG.value)
    with pytest.raises(IllegalTransition) as raised:
        transition(ticket, TicketStatus.DONE)
    assert "cannot move ticket from 'backlog' to 'done'" in str(raised.value)
    assert "ready" in str(raised.value)
    assert ticket.status == "backlog"


def test_a_terminal_status_accepts_nothing() -> None:
    """``done`` is an end; the message says so instead of listing moves."""
    ticket = Ticket(title="A ticket", status=TicketStatus.DONE.value)
    with pytest.raises(IllegalTransition, match="the allowed moves are: nothing"):
        transition(ticket, TicketStatus.READY)


def test_a_status_outside_the_enum_is_reported() -> None:
    """A corrupt or unknown value fails loudly rather than moving on."""
    ticket = Ticket(title="A ticket", status="bogus")
    with pytest.raises(UnknownStatus, match="not a TicketStatus"):
        transition(ticket, TicketStatus.READY)


def test_a_run_walks_the_graph_and_stops_when_completed() -> None:
    """The happy path of ARCHITECTURE section 4, then a terminal refusal."""
    run = Run(status=RunStatus.PENDING.value)
    for status in (
        RunStatus.PLANNING,
        RunStatus.AWAITING_APPROVAL,
        RunStatus.IMPLEMENTING,
        RunStatus.REVIEWING,
        RunStatus.PR_OPEN,
        RunStatus.COMPLETED,
    ):
        transition(run, status)
    assert run.status == "completed"
    with pytest.raises(IllegalTransition):
        transition(run, RunStatus.IMPLEMENTING)


def test_a_rejected_step_can_be_retried() -> None:
    """Doc 12 sends a rejected step back for another attempt."""
    step = RunStep(run_id="run", step_index=0, title="Do it", status=RunStepStatus.FAILED.value)
    transition(step, RunStepStatus.PENDING)
    assert step.status == "pending"


def test_an_illegal_step_move_raises() -> None:
    """A completed step does not start again."""
    step = RunStep(run_id="run", step_index=0, title="Do it", status=RunStepStatus.COMPLETED.value)
    with pytest.raises(IllegalTransition, match="run_step"):
        transition(step, RunStepStatus.RUNNING)


def test_a_row_without_a_machine_is_a_type_error() -> None:
    """Only the three status-bearing rows have a machine (a guard, not a feature)."""
    approval = cast("Ticket", Approval(run_id="run"))
    with pytest.raises(TypeError, match="Approval has no status machine"):
        transition(approval, TicketStatus.DONE)


@pytest.mark.parametrize(
    ("transitions", "status_type"),
    [
        (TICKET_TRANSITIONS, TicketStatus),
        (RUN_TRANSITIONS, RunStatus),
        (RUN_STEP_TRANSITIONS, RunStepStatus),
    ],
)
def test_every_status_is_in_the_table(
    transitions: Mapping[StrEnum, frozenset[StrEnum]], status_type: type[StrEnum]
) -> None:
    """Every member, terminal ones included, has an entry: none can be forgotten."""
    assert set(transitions) == {member for member in status_type}
