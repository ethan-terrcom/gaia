"""UUIDv7 identifiers, as strings.

Owned by doc 03, which asks for "the ``uuid6`` package or a small helper". This
is the helper: twenty lines instead of a dependency, with RFC 9562's layout -
a 48-bit big-endian Unix timestamp in milliseconds, a 4-bit version (7), 12
random bits, the 2-bit RFC 4122 variant and 62 random bits.

Time-ordered keys sort by creation time, which keeps SQLite's B-tree inserts
local instead of scattering them. UUIDv7 is *not* monotonic inside one
millisecond (the 2026-11-12 review, section 4), which is why ``event.seq`` is a
separate integer column rather than a UUID comparison.
"""

from __future__ import annotations

import os
import time
import uuid

_VERSION = 0x7
#: 0b10 in the two most significant bits of the low half (RFC 4122 variant).
_VARIANT = 0b10 << 62
_MAX_48_BITS = (1 << 48) - 1
_MAX_62_BITS = (1 << 62) - 1


def uuid7() -> str:
    """Return a new UUIDv7 as a canonical lowercase string."""
    timestamp_ms = time.time_ns() // 1_000_000
    random_bits = int.from_bytes(os.urandom(10), "big")
    high = ((timestamp_ms & _MAX_48_BITS) << 16) | (_VERSION << 12) | (random_bits >> 68)
    low = _VARIANT | (random_bits & _MAX_62_BITS)
    return str(uuid.UUID(int=(high << 64) | low))
