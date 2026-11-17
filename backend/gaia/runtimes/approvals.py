"""Command approvals that stay inside one agent turn (doc 10a requirement 8).

A runtime's permission hook calls :meth:`ApprovalBroker.request` and awaits it
while the session stays alive. The in-memory broker is for tests and fakes; the
database broker persists the request, publishes ``approval.requested``, and
wakes an in-process waiter when doc 14's HTTP route calls :meth:`decide`.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from gaia.db.engine import session_scope
from gaia.db.ids import uuid7
from gaia.db.models import Approval
from gaia.db.repo import ScopedRepo
from gaia.db.types import utcnow
from gaia.domain.enums import ApprovalKind, ApprovalStatus, EventType
from gaia.runtimes.base import AgentTask
from gaia.services.events import EventBus


@dataclass(frozen=True)
class ApprovalDecision:
    """The answer to one approval request. ``False`` is the safe default.

    ``auto`` is true when auto mode answered instead of a human.
    """

    approved: bool
    reason: str
    auto: bool = False


class ApprovalNotPending(Exception):
    """``decide`` was called on an approval that is no longer pending."""

    def __init__(self, approval_id: str, status: str) -> None:
        self.approval_id = approval_id
        self.status = status
        super().__init__(f"approval {approval_id!r} is {status}, not pending")


class ApprovalBroker(Protocol):
    """Ask a human to approve one command, without leaving the agent turn."""

    async def request(
        self,
        task: AgentTask,
        *,
        kind: ApprovalKind,
        payload: Mapping[str, Any],
        timeout_s: float,
    ) -> ApprovalDecision:
        """Return the decision, or deny with ``"timed out"`` after ``timeout_s``."""
        ...


def _canonical_json(payload: Mapping[str, Any]) -> str:
    """The canonical JSON form used for the approval idempotency key."""
    return json.dumps(dict(payload), sort_keys=True, separators=(",", ":"))


def _idempotency_key(task: AgentTask, payload: Mapping[str, Any]) -> str:
    """A stable key for one in-turn command approval.

    The payload is canonicalised so the same command asked twice inside one task
    upserts the same row instead of piling up duplicate questions.
    """
    digest = hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()[:16]
    return f"cmd:{task.task_id}:{digest}"


class InMemoryApprovalBroker:
    """An in-process broker for tests and :class:`~gaia.runtimes.fake.FakeRuntime`."""

    def __init__(self) -> None:
        self._waiters: dict[str, asyncio.Event] = {}
        self._decisions: dict[str, ApprovalDecision] = {}

    async def request(
        self,
        task: AgentTask,
        *,
        kind: ApprovalKind,
        payload: Mapping[str, Any],
        timeout_s: float,
    ) -> ApprovalDecision:
        """Wait until :meth:`decide` answers, or deny on timeout."""
        del kind, payload  # the fake broker intentionally does not persist these
        approval_id = uuid7()
        event = asyncio.Event()
        self._waiters[approval_id] = event
        try:
            await asyncio.wait_for(event.wait(), timeout_s)
        except TimeoutError:
            self._waiters.pop(approval_id, None)
            self._decisions.pop(approval_id, None)
            return ApprovalDecision(False, "timed out")
        decision = self._decisions.pop(approval_id)
        self._waiters.pop(approval_id, None)
        return decision

    def pending(self) -> tuple[str, ...]:
        """The ids of requests that have been made but not decided yet."""
        return tuple(
            approval_id for approval_id, event in self._waiters.items() if not event.is_set()
        )

    def decide(self, approval_id: str, approved: bool, reason: str) -> None:
        """Answer one pending request. Unknown or already-decided ids raise."""
        event = self._waiters.get(approval_id)
        if event is None or event.is_set():
            raise KeyError(f"no pending approval {approval_id!r}")
        self._decisions[approval_id] = ApprovalDecision(approved, reason)
        event.set()


class DbApprovalBroker:
    """Persist an approval, publish events, and await the in-process decision."""

    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        bus: EventBus,
    ) -> None:
        self.sessions = sessions
        self.bus = bus
        self._waiters: dict[str, asyncio.Event] = {}

    async def request(
        self,
        task: AgentTask,
        *,
        kind: ApprovalKind,
        payload: Mapping[str, Any],
        timeout_s: float,
    ) -> ApprovalDecision:
        """Upsert and publish ``approval.requested``, then await the decision.

        The same request in the same task (same idempotency key) reuses its row:
        an approved or rejected answer is returned at once, so an agent cannot
        re-ask until a refusal flips (lead review 10a, D3). Only an expired row
        is asked again.
        """
        idempotency_key = _idempotency_key(task, payload)
        approval_id, decided = await self._upsert(task, kind, payload, idempotency_key)
        if decided is not None:
            return decided
        # Register the waiter *before* publishing: a decision can arrive while
        # the publish is still awaiting, and it must not be lost (D1).
        event = asyncio.Event()
        self._waiters[approval_id] = event
        try:
            await self.bus.publish(
                workspace_id=task.workspace_id,
                type=EventType.APPROVAL_REQUESTED,
                payload={"approval_id": approval_id, "kind": kind.value},
                run_id=task.run_id,
                agent_id=task.agent_id,
            )
            await asyncio.wait_for(event.wait(), timeout_s)
        except TimeoutError:
            await self._mark_expired(task.workspace_id, approval_id)
        finally:
            self._waiters.pop(approval_id, None)
        # The row is the truth: it reflects a decision that raced the timeout.
        return _decision_from_row(await self._get(task.workspace_id, approval_id))

    async def decide(
        self,
        workspace_id: str,
        approval_id: str,
        approved: bool,
        comment: str,
        user_id: str,
    ) -> None:
        """Record and publish the decision, then wake the waiting runtime."""
        status_value = ApprovalStatus.APPROVED.value if approved else ApprovalStatus.REJECTED.value
        async with session_scope(self.sessions) as session:
            repo = ScopedRepo(session, Approval, workspace_id)
            approval = await repo.get(approval_id)
            if approval is None:
                raise LookupError(f"no approval {approval_id!r}")
            if approval.status != ApprovalStatus.PENDING.value:
                # A late click after the runtime timed out (or a double
                # submit) must not rewrite history (D2). Doc 14 maps this to 409.
                raise ApprovalNotPending(approval_id, approval.status)
            await repo.update(
                approval_id,
                status=status_value,
                decided_by=user_id,
                decided_at=utcnow(),
                comment=comment,
            )
        await self.bus.publish(
            workspace_id=workspace_id,
            type=EventType.APPROVAL_DECIDED,
            payload={"approval_id": approval_id, "status": status_value, "comment": comment},
            run_id=approval.run_id,
            agent_id=None,
        )
        waiter = self._waiters.get(approval_id)
        if waiter is not None:
            waiter.set()

    async def _upsert(
        self,
        task: AgentTask,
        kind: ApprovalKind,
        payload: Mapping[str, Any],
        idempotency_key: str,
    ) -> tuple[str, ApprovalDecision | None]:
        """Return the row id for this request, and its decision if already made.

        A pending row is waited on again (a resumed runtime), an expired row is
        reset to pending and asked again, and an approved or rejected row is
        answered from the row.
        """
        async with session_scope(self.sessions) as session:
            repo = ScopedRepo(session, Approval, task.workspace_id)
            existing = await repo.list(run_id=task.run_id, idempotency_key=idempotency_key)
            if existing:
                row = existing[0]
                if row.status in (ApprovalStatus.APPROVED.value, ApprovalStatus.REJECTED.value):
                    return row.id, _decision_from_row(row)
                if row.status == ApprovalStatus.PENDING.value:
                    return row.id, None
                await repo.update(
                    row.id,
                    kind=kind.value,
                    payload_json=dict(payload),
                    status=ApprovalStatus.PENDING.value,
                    decided_by=None,
                    decided_at=None,
                    comment=None,
                )
                return row.id, None
            row = await repo.create(
                run_id=task.run_id,
                idempotency_key=idempotency_key,
                kind=kind.value,
                payload_json=dict(payload),
                status=ApprovalStatus.PENDING.value,
            )
            return row.id, None

    async def _mark_expired(self, workspace_id: str, approval_id: str) -> None:
        """Mark a still-pending approval ``expired`` after a timeout."""
        async with session_scope(self.sessions) as session:
            repo = ScopedRepo(session, Approval, workspace_id)
            approval = await repo.get(approval_id)
            if approval is not None and approval.status == ApprovalStatus.PENDING.value:
                await repo.update(approval_id, status=ApprovalStatus.EXPIRED.value)

    async def _get(self, workspace_id: str, approval_id: str) -> Approval | None:
        """Read one approval in a fresh session."""
        async with self.sessions() as session:
            return await ScopedRepo(session, Approval, workspace_id).get(approval_id)


def _decision_from_row(approval: Approval | None) -> ApprovalDecision:
    """Map a persisted approval row to the runtime-facing decision."""
    if approval is None:
        return ApprovalDecision(False, "approval not found")
    if approval.status == ApprovalStatus.APPROVED.value:
        return ApprovalDecision(True, approval.comment or "approved")
    if approval.status == ApprovalStatus.EXPIRED.value:
        return ApprovalDecision(False, "timed out")
    return ApprovalDecision(False, approval.comment or approval.status)
