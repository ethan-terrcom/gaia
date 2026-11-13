"""The legal status transitions, in one dict (doc 03 task 4).

Services ask :func:`transition` to move a row instead of assigning
``row.status = ...``, so an illegal move - a finished run going back to
``implementing``, a ticket jumping from ``backlog`` to ``done`` - raises at the
line that made the mistake rather than in a later query.

The tables follow the graph in ARCHITECTURE section 4, with three reading rules:

- **A move to the current status is a no-op, not an error.** Nodes write their
  rows idempotently, so a resumed run legitimately re-applies a status it
  already set (the 2026-11-12 review, section 3.2).
- **Terminal statuses accept nothing.** ``completed``, ``failed``, ``done`` and
  ``cancelled`` are ends: doc 12 resumes a failed run from its checkpoint, which
  re-enters an existing status instead of reviving it from ``failed``.
- **``awaiting_approval`` is shared by every gate.** ARCHITECTURE section 4 keeps
  plan, escalation, budget, ``pr_open`` and ``merge`` as graph interrupts; which
  gate is open is on the ``approval`` row, not on the run's status.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol

from gaia.db.models import Run, RunStep, Ticket
from gaia.domain.enums import RunStatus, RunStepStatus, TicketStatus

TICKET_TRANSITIONS: Mapping[TicketStatus, frozenset[TicketStatus]] = {
    TicketStatus.BACKLOG: frozenset({TicketStatus.READY, TicketStatus.CANCELLED}),
    TicketStatus.READY: frozenset(
        {TicketStatus.BACKLOG, TicketStatus.IN_PROGRESS, TicketStatus.CANCELLED}
    ),
    TicketStatus.IN_PROGRESS: frozenset(
        {TicketStatus.READY, TicketStatus.IN_REVIEW, TicketStatus.CANCELLED}
    ),
    TicketStatus.IN_REVIEW: frozenset(
        {TicketStatus.IN_PROGRESS, TicketStatus.DONE, TicketStatus.CANCELLED}
    ),
    TicketStatus.DONE: frozenset(),
    TicketStatus.CANCELLED: frozenset(),
}

RUN_TRANSITIONS: Mapping[RunStatus, frozenset[RunStatus]] = {
    RunStatus.PENDING: frozenset({RunStatus.PLANNING, RunStatus.FAILED, RunStatus.CANCELLED}),
    RunStatus.PLANNING: frozenset(
        {RunStatus.AWAITING_APPROVAL, RunStatus.IMPLEMENTING, RunStatus.FAILED, RunStatus.CANCELLED}
    ),
    RunStatus.AWAITING_APPROVAL: frozenset(
        {
            RunStatus.PLANNING,
            RunStatus.IMPLEMENTING,
            RunStatus.REVIEWING,
            RunStatus.PR_OPEN,
            RunStatus.PAUSED_BUDGET,
            RunStatus.FAILED,
            RunStatus.CANCELLED,
        }
    ),
    RunStatus.IMPLEMENTING: frozenset(
        {
            RunStatus.REVIEWING,
            RunStatus.AWAITING_APPROVAL,
            RunStatus.PAUSED_BUDGET,
            RunStatus.FAILED,
            RunStatus.CANCELLED,
        }
    ),
    RunStatus.REVIEWING: frozenset(
        {
            RunStatus.IMPLEMENTING,
            RunStatus.AWAITING_APPROVAL,
            RunStatus.PR_OPEN,
            RunStatus.PAUSED_BUDGET,
            RunStatus.FAILED,
            RunStatus.CANCELLED,
        }
    ),
    RunStatus.PR_OPEN: frozenset(
        {
            RunStatus.REVIEWING,
            RunStatus.AWAITING_APPROVAL,
            RunStatus.COMPLETED,
            RunStatus.FAILED,
            RunStatus.CANCELLED,
        }
    ),
    RunStatus.PAUSED_BUDGET: frozenset(
        {
            RunStatus.PLANNING,
            RunStatus.IMPLEMENTING,
            RunStatus.REVIEWING,
            RunStatus.AWAITING_APPROVAL,
            RunStatus.FAILED,
            RunStatus.CANCELLED,
        }
    ),
    RunStatus.FAILED: frozenset(),
    RunStatus.COMPLETED: frozenset(),
    RunStatus.CANCELLED: frozenset(),
}

RUN_STEP_TRANSITIONS: Mapping[RunStepStatus, frozenset[RunStepStatus]] = {
    RunStepStatus.PENDING: frozenset({RunStepStatus.RUNNING, RunStepStatus.CANCELLED}),
    RunStepStatus.RUNNING: frozenset(
        {RunStepStatus.COMPLETED, RunStepStatus.FAILED, RunStepStatus.CANCELLED}
    ),
    # A rejected step is retried: the graph sends it back for another attempt.
    RunStepStatus.FAILED: frozenset({RunStepStatus.PENDING, RunStepStatus.CANCELLED}),
    RunStepStatus.COMPLETED: frozenset(),
    RunStepStatus.CANCELLED: frozenset(),
}


class IllegalTransition(ValueError):
    """The move is not in the table for this kind of row."""

    def __init__(
        self, name: str, current: str, new_status: str, allowed: Iterable[StrEnum]
    ) -> None:
        self.name = name
        self.current = current
        self.new_status = new_status
        moves = ", ".join(sorted(member.value for member in allowed)) or "nothing"
        super().__init__(
            f"cannot move {name} from {current!r} to {new_status!r}; "
            f"from {current!r} the allowed moves are: {moves}"
        )


class UnknownStatus(ValueError):
    """The row holds a status that is not a member of its enum."""


class _StatusRow(Protocol):
    """Anything with a writable text ``status`` column."""

    status: str


@dataclass(frozen=True)
class _Machine[StatusT: StrEnum]:
    """One row type's enum and transition table."""

    name: str
    status_type: type[StatusT]
    transitions: Mapping[StatusT, frozenset[StatusT]]

    def apply(self, entity: _StatusRow, new_status: str) -> None:
        try:
            current = self.status_type(entity.status)
        except ValueError:
            raise UnknownStatus(
                f"{self.name} has status {entity.status!r}, "
                f"which is not a {self.status_type.__name__}"
            ) from None
        if new_status == current:
            # Idempotent re-application: a resumed node writes its row twice.
            return
        allowed = self.transitions[current]
        if new_status not in allowed:
            raise IllegalTransition(self.name, current.value, new_status, allowed)
        entity.status = new_status


_BY_MODEL: Mapping[type[Ticket | Run | RunStep], _Machine[Any]] = {
    Ticket: _Machine("ticket", TicketStatus, TICKET_TRANSITIONS),
    Run: _Machine("run", RunStatus, RUN_TRANSITIONS),
    RunStep: _Machine("run_step", RunStepStatus, RUN_STEP_TRANSITIONS),
}


def transition(entity: Ticket | Run | RunStep, new_status: str) -> None:
    """Move ``entity`` to ``new_status``, or raise :class:`IllegalTransition`.

    ``new_status`` is a :class:`~gaia.domain.enums.TicketStatus` member or the
    plain string of one: both compare equal, and the row stores the string.
    """
    try:
        machine = _BY_MODEL[type(entity)]
    except KeyError:
        raise TypeError(f"{type(entity).__name__} has no status machine") from None
    machine.apply(entity, str(new_status))
