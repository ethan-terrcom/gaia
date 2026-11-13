"""The UTC column type and clock (doc 03 task 1)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

from sqlalchemy.dialects import sqlite

from gaia.db.types import UtcDateTime, utcnow

DIALECT = sqlite.dialect()


def test_utcnow_is_aware_and_utc() -> None:
    """The clock hands out aware UTC, so a row never stores a naive time."""
    now = utcnow()
    assert now.tzinfo is UTC
    assert now.utcoffset() == timedelta(0)


def test_bind_converts_to_utc_and_assumes_utc_for_naive_values() -> None:
    """A naive value is redundant to treat as anything but UTC, so it is UTC."""
    column = UtcDateTime()
    naive = datetime(2026, 11, 13, 9, 30)
    offset = datetime(2026, 11, 13, 11, 30, tzinfo=timezone(timedelta(hours=2)))

    assert column.process_bind_param(naive, DIALECT) == naive.replace(tzinfo=UTC)
    assert column.process_bind_param(offset, DIALECT) == datetime(2026, 11, 13, 9, 30, tzinfo=UTC)


def test_result_reattaches_utc_to_what_sqlite_returns() -> None:
    """SQLite drops the offset, so the read side puts UTC back."""
    column = UtcDateTime()
    stored = datetime(2026, 11, 13, 9, 30)
    assert column.process_result_value(stored, DIALECT) == stored.replace(tzinfo=UTC)


def test_an_aware_value_is_kept_aware() -> None:
    """A value that already carries an offset is converted, not replaced."""
    column = UtcDateTime()
    offset = datetime(2026, 11, 13, 11, 30, tzinfo=timezone(timedelta(hours=2)))
    assert column.process_result_value(offset, DIALECT) == datetime(2026, 11, 13, 9, 30, tzinfo=UTC)


def test_both_sides_pass_none_through() -> None:
    """``NULL`` stays ``NULL``: the nullable timestamp columns rely on it."""
    column = UtcDateTime()
    assert column.process_bind_param(None, DIALECT) is None
    assert column.process_result_value(None, DIALECT) is None
