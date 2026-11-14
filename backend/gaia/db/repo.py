"""Workspace-scoped repositories: the only way to query a tenant table.

Owned by doc 03 tasks 5 and 6. ARCHITECTURE section 7 says the ``workspace_id``
filter lives in a repository helper "never ad hoc", so:

- :class:`ScopedRepo` builds every statement with ``workspace_id = <the repo's>``
  baked in. A row from another workspace is invisible: :meth:`ScopedRepo.get`
  returns ``None`` and :meth:`ScopedRepo.update` raises, rather than leaking the
  existence of someone else's data.
- :class:`AppendOnlyRepo` is the same idea with a smaller surface. ``event`` and
  ``audit_log`` get ``append`` and ``list`` and nothing else, because a trail
  that can be rewritten is not a trail.
- Nothing outside ``gaia/db/`` should call ``select()`` on a tenant table. The
  filter names :meth:`ScopedRepo.list` takes are column names chosen by a
  service, never a string from a request or from a model.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import Select, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from gaia.db.models import AuditLog, Base, Event, LlmUsage, Run, RunToken, Ticket
from gaia.db.types import utcnow


class WorkspaceMismatch(ValueError):
    """A write tried to put a row in a workspace the repo does not own."""

    def __init__(self, model: str, expected: str, given: str) -> None:
        self.model = model
        self.expected = expected
        self.given = given
        super().__init__(
            f"cannot write {model} into workspace {given!r} from a repo scoped to {expected!r}"
        )


class NotFound(LookupError):
    """No row with that id exists in this workspace."""

    def __init__(self, model: str, row_id: str) -> None:
        self.model = model
        self.row_id = row_id
        super().__init__(f"no {model} with id {row_id!r} in this workspace")


def _workspace_of(row: Any) -> str:
    """Read a row's tenant id.

    ``row`` is typed ``Any`` on purpose: every model gets ``workspace_id`` from
    :class:`gaia.db.models.WorkspaceScoped`, but the repository is generic over
    the model class, and mypy cannot see a mixin's column through a ``TypeVar``.
    """
    workspace_id: str = row.workspace_id
    return workspace_id


class RunTokenRepo:
    """Unscoped run-token lookups and idempotent revocation (doc 07b).

    Resolution deliberately does **not** filter by workspace: the SHA-256 token
    hash is globally unique, and the token row itself is what proves the
    workspace. ``revoke_*`` only sets ``revoked_at`` on live tokens, so calling
    either method twice is a no-op.
    """

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get(self, token_id: str) -> RunToken | None:
        """Return a token by id, for tests and administrative revocation."""
        return await self.session.get(RunToken, token_id)

    async def get_by_hash(self, token_hash: str) -> RunToken | None:
        """Return the token whose hash matches, if any (no workspace filter)."""
        statement = select(RunToken).where(RunToken.token_hash == token_hash)
        return (await self.session.scalars(statement)).one_or_none()

    async def revoke_by_id(self, workspace_id: str, token_id: str) -> None:
        """Revoke one token by id, leaving an already-revoked token untouched.

        Scoped by workspace like every other write (review 01-05, F5): a token
        id from another workspace is a no-op, not a revocation.
        """
        statement = (
            update(RunToken)
            .where(
                RunToken.workspace_id == workspace_id,
                RunToken.id == token_id,
                RunToken.revoked_at.is_(None),
            )
            .values(revoked_at=utcnow())
        )
        await self.session.execute(statement)

    async def revoke_for_run(self, workspace_id: str, run_id: str) -> None:
        """Revoke every live token minted for one run in one workspace."""
        statement = (
            update(RunToken)
            .where(
                RunToken.workspace_id == workspace_id,
                RunToken.run_id == run_id,
                RunToken.revoked_at.is_(None),
            )
            .values(revoked_at=utcnow())
        )
        await self.session.execute(statement)


async def add_run_cost(session: AsyncSession, run_id: str, cost_usd: Decimal) -> None:
    """Add ``cost_usd`` to ``run.cost_usd`` atomically (doc 07b).

    The read-modify-write alternative would lose a concurrent usage row; the
    database does the addition instead.
    """
    statement = update(Run).where(Run.id == run_id).values(cost_usd=Run.cost_usd + cost_usd)
    await session.execute(statement)


async def daily_llm_usage_cost_usd(
    session: AsyncSession, workspace_id: str, *, start: datetime, end: datetime
) -> Decimal:
    """Return the sum of ``llm_usage.cost_usd`` in the half-open UTC day.

    The query lives here because the "never build SQL outside ``gaia/db``" rule
    applies just as much to aggregates as to row reads.
    """
    total = await session.scalar(
        select(func.coalesce(func.sum(LlmUsage.cost_usd), 0)).where(
            LlmUsage.workspace_id == workspace_id,
            LlmUsage.created_at >= start,
            LlmUsage.created_at < end,
        )
    )
    # SQLite returns a float for SUM over NUMERIC; go through str so the
    # Decimal is the value printed, not the float's binary expansion.
    return Decimal(str(total or 0))


class Repo[ModelT: Base]:
    """Query building shared by the two public repositories.

    Not used directly: it has no public read or write method, so a service
    cannot get hold of an unscoped statement through it.
    """

    #: Column ``list`` orders by. ``id`` is UUIDv7, so it is creation order.
    order_by = "id"

    def __init__(self, session: AsyncSession, model: type[ModelT], workspace_id: str) -> None:
        self.session = session
        self.model = model
        self.workspace_id = workspace_id

    @property
    def _table(self) -> Any:
        """The model, as the place to reach columns from (mypy-friendly)."""
        return self.model

    def _select(self, **filters: Any) -> Select[Any]:
        """A statement filtered by this repo's workspace and the given columns."""
        statement = select(self.model).where(self._table.workspace_id == self.workspace_id)
        conditions = [getattr(self._table, name) == value for name, value in filters.items()]
        if conditions:
            statement = statement.where(*conditions)
        return statement.order_by(getattr(self._table, self.order_by))

    def _check_workspace(self, values: dict[str, Any]) -> None:
        """Refuse a ``workspace_id`` in ``values`` that is not the repo's own."""
        given = values.pop("workspace_id", self.workspace_id)
        if given != self.workspace_id:
            raise WorkspaceMismatch(self.model.__name__, self.workspace_id, given)

    async def _insert(self, **values: Any) -> ModelT:
        """Build, add and flush a row; validation happens in the caller."""
        self._check_workspace(values)
        values["workspace_id"] = self.workspace_id
        row = self.model(**values)
        self.session.add(row)
        await self.session.flush()
        return row


class ScopedRepo[ModelT: Base](Repo[ModelT]):
    """``get``, ``list``, ``create`` and ``update`` inside one workspace."""

    async def get(self, row_id: str) -> ModelT | None:
        """Return the row with ``row_id``, or ``None`` if it is not ours.

        A row that exists in another workspace is reported exactly like a row
        that does not exist at all.
        """
        row = await self.session.get(self.model, row_id)
        if row is None or _workspace_of(row) != self.workspace_id:
            return None
        return row

    async def list(
        self,
        *,
        limit: int | None = None,
        offset: int = 0,
        after_id: str | None = None,
        **filters: Any,
    ) -> list[ModelT]:
        """Rows in this workspace, optionally narrowed by equality on columns.

        ``after_id`` is the UUIDv7 cursor: rows with an id greater than the
        cursor, which is creation order. Like every other condition, it is an
        SQLAlchemy expression compiled with a bound value, never a string built
        into SQL.
        """
        statement = self._select(**filters)
        if after_id is not None:
            statement = statement.where(self._table.id > after_id)
        statement = statement.offset(offset)
        if limit is not None:
            statement = statement.limit(limit)
        return list((await self.session.scalars(statement)).all())

    async def create(self, **values: Any) -> ModelT:
        """Insert a row in this workspace and return it, defaults applied."""
        return await self._insert(**values)

    async def update(self, row_id: str, **values: Any) -> ModelT:
        """Set columns on an existing row of this workspace."""
        row = await self.get(row_id)
        if row is None:
            raise NotFound(self.model.__name__, row_id)
        self._check_workspace(values)
        for name, value in values.items():
            setattr(row, name, value)
        await self.session.flush()
        return row

    async def delete(self, row_id: str) -> ModelT:
        """Delete an existing row of this workspace and return it."""
        row = await self.get(row_id)
        if row is None:
            raise NotFound(self.model.__name__, row_id)
        await self.session.delete(row)
        await self.session.flush()
        return row


class AppendOnlyRepo[ModelT: Base](Repo[ModelT]):
    """``append`` and ``list`` for ``event`` and ``audit_log`` (doc 03 task 6)."""

    async def append(self, **values: Any) -> ModelT:
        """Append a row. There is no update and no delete, on purpose."""
        return await self._insert(**values)

    async def list(self, *, limit: int | None = None, **filters: Any) -> list[ModelT]:
        """Rows in this workspace, oldest first."""
        statement = self._select(**filters)
        if limit is not None:
            statement = statement.limit(limit)
        return list((await self.session.scalars(statement)).all())


class EventRepo(AppendOnlyRepo[Event]):
    """The event log, ordered by ``seq`` so a client can replay from a cursor.

    ``seq`` is what SSE's ``Last-Event-ID`` carries (doc 13), which is why it is
    the order rather than ``id`` or ``ts``.
    """

    order_by = "seq"

    async def list_after(
        self,
        *,
        after_seq: int = 0,
        limit: int | None = None,
        run_id: str | None = None,
    ) -> list[Event]:
        """Events in this workspace with ``seq`` above ``after_seq``, oldest first.

        ``run_id`` optionally narrows the replay to one run. Both the SSE
        reconnect path (no run filter) and the per-run history endpoint use this
        cursor, so the ordering and the ``>`` comparison live in exactly one
        method.
        """
        statement = self._select() if run_id is None else self._select(run_id=run_id)
        statement = statement.where(self._table.seq > after_seq)
        if limit is not None:
            statement = statement.limit(limit)
        return list((await self.session.scalars(statement)).all())

    async def list_for_run(
        self, run_id: str, *, after_seq: int = 0, limit: int | None = None
    ) -> list[Event]:
        """Events of one run with ``seq`` above ``after_seq``, oldest first."""
        return await self.list_after(after_seq=after_seq, limit=limit, run_id=run_id)


class TicketRepo(ScopedRepo[Ticket]):
    """Ticket queries: filters plus the cursor-paginated ``search`` for doc 06."""

    async def search(
        self,
        *,
        status: str | None = None,
        repo_id: str | None = None,
        q: str | None = None,
        after_id: str | None = None,
        limit: int | None = None,
    ) -> list[Ticket]:
        """Tickets matching the dashboard filters, ordered by id.

        ``q`` matches ``title`` case-insensitively. ``%`` and ``_`` in ``q`` are
        escaped before the pattern is passed as a bound parameter, so a search
        term cannot change the shape of the ``LIKE`` expression.
        """
        statement = self._select()
        if status is not None:
            statement = statement.where(self._table.status == status)
        if repo_id is not None:
            statement = statement.where(self._table.repo_id == repo_id)
        if q:
            escaped = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            statement = statement.where(self._table.title.ilike(f"%{escaped}%", escape="\\"))
        if after_id is not None:
            statement = statement.where(self._table.id > after_id)
        if limit is not None:
            statement = statement.limit(limit)
        return list((await self.session.scalars(statement)).all())


class AuditLogRepo(AppendOnlyRepo[AuditLog]):
    """The audit trail, oldest first."""

    order_by = "ts"
