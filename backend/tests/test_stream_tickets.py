"""Tests for single-use SSE stream tickets (doc 13)."""

from __future__ import annotations

from gaia.api.auth import Principal
from gaia.security.redaction import redact
from gaia.services.stream_tickets import StreamTickets

PRINCIPAL = Principal(workspace_id="ws-1", user_id="user-1")


def test_a_ticket_is_single_use() -> None:
    tickets = StreamTickets(ttl_s=60)

    ticket = tickets.issue(PRINCIPAL)

    assert tickets.redeem(ticket) == PRINCIPAL
    assert tickets.redeem(ticket) is None


def test_an_unknown_ticket_is_none() -> None:
    tickets = StreamTickets(ttl_s=60)

    assert tickets.redeem("not-a-ticket") is None


def test_a_ticket_expires() -> None:
    now = 0.0

    def clock() -> float:
        return now

    tickets = StreamTickets(ttl_s=10, now=clock)
    ticket = tickets.issue(PRINCIPAL)

    now = 10.0

    assert tickets.redeem(ticket) is None


def test_issue_purges_expired_entries() -> None:
    now = 0.0

    def clock() -> float:
        return now

    tickets = StreamTickets(ttl_s=10, now=clock)
    first = tickets.issue(PRINCIPAL)

    now = 11.0
    second = tickets.issue(PRINCIPAL)

    assert tickets.redeem(first) is None
    assert tickets.redeem(second) == PRINCIPAL


def test_the_ticket_plaintext_is_not_added_to_the_redaction_registry() -> None:
    """Lead change (review 13, E3): the registry is never pruned and is scanned per log line."""
    tickets = StreamTickets(ttl_s=60)

    ticket = tickets.issue(PRINCIPAL)

    assert redact(f"ticket={ticket}") == f"ticket={ticket}"
