"""The audit trail for every tool call (doc 09b task 4).

The registry writes one :class:`AuditEntry` for every call, whatever the model
did, and the sinks in this module decide where the row goes. Two properties
matter:

- **Append-only, in its own transaction.** The database sink opens its own short
  transaction like :class:`~gaia.services.events.EventBus` does, so an audit row
  survives even when the caller's surrounding transaction rolls back. The
  append-only repository has no update or delete, so a trail cannot be rewritten.
- **A failing audit write never reaches the tool.** If the database hiccups the
  agent must keep running; the failure is logged and swallowed. An agent must
  not be able to break its own audit trail by making a tool fail, and a database
  problem must not crash a turn.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from gaia.db.engine import session_scope
from gaia.db.models import AuditLog
from gaia.db.repo import AuditLogRepo
from gaia.security.redaction import redact

logger = structlog.get_logger(__name__)

#: Longest string that is stored or emitted verbatim. A model-supplied
#: ``content`` value above this is replaced by a hash and a character count.
_MAX_STRING_CHARS = 256

#: Longest list/tuple of strings (an argv) that is stored or emitted verbatim.
_MAX_STRING_ITEMS = 256

#: Nested mappings are summarised to this depth, then replaced with ``"…"``.
_MAX_MAPPING_DEPTH = 3


@dataclass(frozen=True)
class AuditEntry:
    """One append-only record of a tool call.

    ``action`` is ``"tool.<name>"``. ``target`` is the relative path for a file
    tool or ``shlex.join(argv)`` for a command, capped at 500 characters; it is
    what a human scans first. ``details`` carries the structured fields
    (:data:`summarize_args` output plus ids, outcome, decision, reason and
    duration).
    """

    workspace_id: str
    actor: str
    action: str
    target: str | None
    details: Mapping[str, Any]


class AuditSink(Protocol):
    """Write one :class:`AuditEntry` somewhere durable."""

    async def record(self, entry: AuditEntry) -> None:
        """Persist ``entry``. Implementations must not raise for a DB failure."""
        ...


class DbAuditSink:
    """Append audit rows through :class:`AuditLogRepo` in a short transaction."""

    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self.sessions = sessions

    async def record(self, entry: AuditEntry) -> None:
        """Write one row, logging and swallowing any database failure."""
        try:
            async with session_scope(self.sessions) as session:
                await AuditLogRepo(session, AuditLog, entry.workspace_id).append(
                    actor=entry.actor,
                    action=entry.action,
                    target=entry.target,
                    details_json=dict(entry.details),
                )
        except Exception:
            logger.exception(
                "audit.write_failed",
                workspace_id=entry.workspace_id,
                actor=entry.actor,
                action=entry.action,
            )


class InMemoryAuditSink:
    """An in-memory sink for tests."""

    def __init__(self) -> None:
        self.entries: list[AuditEntry] = []

    async def record(self, entry: AuditEntry) -> None:
        self.entries.append(entry)


def summarize_args(args: Mapping[str, Any]) -> dict[str, Any]:
    """Return a redacted, bounded copy of one tool call's arguments.

    The rules exist so ``write_file`` content and any other large model-supplied
    value never reaches the database or an event verbatim:

    - a ``str`` over 256 characters becomes ``{"sha256": ..., "chars": ...}``;
    - other strings pass through :func:`gaia.security.redaction.redact`;
    - lists/tuples of strings are redacted element by element, and over 256
      items are replaced by a hash;
    - nested mappings recurse to depth 3, then become ``"…"``;
    - numbers, booleans and ``None`` pass through unchanged.
    """
    return {key: _summarize_value(value, depth=1) for key, value in args.items()}


def _summarize_value(value: Any, *, depth: int) -> Any:
    if isinstance(value, str):
        if len(value) > _MAX_STRING_CHARS:
            return _hashed(value, "chars")
        return redact(value)
    if isinstance(value, (list, tuple)):
        if _all_strings(value):
            if len(value) > _MAX_STRING_ITEMS:
                return _hashed("".join(value), "items")
            return type(value)(redact(item) for item in value)
        if depth >= _MAX_MAPPING_DEPTH:
            return "…"
        return type(value)(_summarize_value(item, depth=depth + 1) for item in value)
    if isinstance(value, Mapping):
        if depth >= _MAX_MAPPING_DEPTH:
            return "…"
        return {key: _summarize_value(item, depth=depth + 1) for key, item in value.items()}
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    # Anything else (for example a Path) is converted to its redacted string.
    return redact(str(value))


def _all_strings(value: list[Any] | tuple[Any, ...]) -> bool:
    return all(isinstance(item, str) for item in value)


def _hashed(value: str, count_field: str) -> dict[str, Any]:
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()
    return {"sha256": digest, count_field: len(value)}
