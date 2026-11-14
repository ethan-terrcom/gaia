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

from typing import Any

from sqlalchemy import Select, select
from sqlalchemy.ext.asyncio import AsyncSession

from gaia.db.models import AuditLog, Base, Event, Ticket


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

    async def list_for_run(
        self, run_id: str, *, after_seq: int = 0, limit: int | None = None
    ) -> list[Event]:
        """Events of one run with ``seq`` above ``after_seq``, oldest first."""
        statement = self._select(run_id=run_id).where(self._table.seq > after_seq)
        if limit is not None:
            statement = statement.limit(limit)
        return list((await self.session.scalars(statement)).all())


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
