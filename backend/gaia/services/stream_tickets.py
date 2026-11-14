"""Single-use tickets for the unauthenticated SSE endpoint.

Owned by doc 13. ``EventSource`` cannot send a bearer header, but putting the
long-lived API token in a URL would leak it into logs and proxy history. So the
UI exchanges the bearer token for a short-lived, single-use stream ticket at
``POST /api/v1/stream/ticket`` and puts only that ticket in the SSE query string.

Only a SHA-256 digest is stored, never the ticket plaintext. The plaintext is
deliberately *not* registered with the redaction layer: the registry is never
pruned and is scanned on every log line, so one entry per reconnect would grow
without bound. Gaia never logs the ticket. Uvicorn's access log can carry it in
the query string, but by then it is already redeemed and worthless; routing
stdlib logs through redaction is review finding F8 (doc 18b).
"""

from __future__ import annotations

import hashlib
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass

from gaia.api.auth import Principal


@dataclass(frozen=True)
class _Ticket:
    workspace_id: str
    user_id: str
    expires_at: float


class StreamTickets:
    """An in-memory, single-use ticket issuer (doc 13)."""

    def __init__(
        self,
        ttl_s: float = 60.0,
        *,
        now: Callable[[], float] = time.monotonic,
    ) -> None:
        self.ttl_s = ttl_s
        self._now = now
        self._tickets: dict[str, _Ticket] = {}

    def issue(self, principal: Principal) -> str:
        """Issue a fresh ticket for ``principal`` and return its plaintext."""
        self._purge_expired()
        ticket = secrets.token_urlsafe(32)
        digest = hashlib.sha256(ticket.encode("utf-8")).hexdigest()
        self._tickets[digest] = _Ticket(
            workspace_id=principal.workspace_id,
            user_id=principal.user_id,
            expires_at=self._now() + self.ttl_s,
        )
        return ticket

    def redeem(self, ticket: str) -> Principal | None:
        """Return the ticket's principal, or ``None`` if it is used/expired.

        The entry is deleted on the first redeem attempt, successful or expired,
        so a ticket can never be used twice. Expired entries are purged first,
        which also makes an expired ticket simply absent.
        """
        self._purge_expired()
        digest = hashlib.sha256(ticket.encode("utf-8")).hexdigest()
        entry = self._tickets.pop(digest, None)
        if entry is None:
            return None
        return Principal(workspace_id=entry.workspace_id, user_id=entry.user_id)

    def _purge_expired(self) -> None:
        """Drop every entry whose expiry has passed."""
        now = self._now()
        expired = [digest for digest, entry in self._tickets.items() if entry.expires_at <= now]
        for digest in expired:
            self._tickets.pop(digest, None)
