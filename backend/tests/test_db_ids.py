"""UUIDv7 helper (doc 03 task 1)."""

from __future__ import annotations

import time
import uuid

from gaia.db import ids


def test_uuid7_has_the_rfc_9562_layout(monkeypatch) -> None:
    """Version 7, RFC 4122 variant, and the timestamp in the top 48 bits."""
    monkeypatch.setattr(time, "time_ns", lambda: 1_760_000_000_123_456_789)
    value = ids.uuid7()

    parsed = uuid.UUID(value)
    assert parsed.version == 7
    assert parsed.variant == uuid.RFC_4122
    # 48-bit big-endian milliseconds at the top, per RFC 9562 section 5.7.
    assert parsed.int >> 80 == 1_760_000_000_123
    assert value == str(parsed)


def test_uuid7_values_are_unique_and_sort_by_time() -> None:
    """Two calls differ, and a later one sorts after an earlier one."""
    first = ids.uuid7()
    time.sleep(0.002)
    second = ids.uuid7()
    assert first != second
    assert first < second
