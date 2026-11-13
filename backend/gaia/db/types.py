"""The two things every timestamped row needs: a UTC clock and a UTC column.

Owned by doc 03 task 1 ("``created_at``, ``updated_at`` (UTC, timezone-aware)").

SQLite has no timezone support: it stores whatever string it is given and hands
back a *naive* ``datetime``. Storing UTC and re-attaching ``timezone.utc`` on the
way out makes the rule true on SQLite and on PostgreSQL alike, and keeps
subtraction between two rows meaningful.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy.engine import Dialect
from sqlalchemy.types import DateTime, TypeDecorator


def utcnow() -> datetime:
    """Return the current time as a timezone-aware UTC ``datetime``."""
    return datetime.now(UTC)


class UtcDateTime(TypeDecorator[datetime]):
    """A ``DATETIME`` that is always stored and returned as aware UTC.

    A naive value means UTC: the only way to get one is to read a row back from
    a database that dropped the offset, and re-writing such a row must not fail.
    """

    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)

    def process_result_value(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)
