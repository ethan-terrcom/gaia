"""Tests for the audit sink and argument summariser (doc 09b task 4)."""

from __future__ import annotations

from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession

from gaia.db.models import AuditLog
from gaia.db.repo import AuditLogRepo
from gaia.security.audit import (
    AuditEntry,
    DbAuditSink,
    InMemoryAuditSink,
    summarize_args,
)
from tests.conftest import Database
from tests.db_builders import make_workspace


def test_summarize_args_hashes_long_strings() -> None:
    value = "x" * 300

    summarized = summarize_args({"content": value})

    assert summarized == {"content": {"sha256": _sha256(value), "chars": 300}}


def test_summarize_args_redacts_short_strings() -> None:
    summarized = summarize_args({"token": "sk-ant-planted-0000000000000000000000"})

    assert summarized == {"token": "[REDACTED]"}


def test_summarize_args_redacts_argv_element_by_element() -> None:
    argv = ["pytest", "sk-ant-planted-0000000000000000000000"]

    summarized = summarize_args({"argv": argv})

    assert summarized == {"argv": ["pytest", "[REDACTED]"]}


def test_summarize_args_hashes_long_argv() -> None:
    argv = ["a", "b"] * 200

    summarized = summarize_args({"argv": argv})

    assert summarized == {"argv": {"sha256": _sha256("".join(argv)), "items": 400}}


def test_summarize_args_caps_nested_mapping_depth() -> None:
    summarized = summarize_args({"a": {"b": {"c": {"d": "secret-value"}}}})

    assert summarized == {"a": {"b": {"c": "…"}}}


def test_summarize_args_passes_through_numbers_bools_and_none() -> None:
    summarized = summarize_args({"n": 3, "f": 1.5, "t": True, "none": None})

    assert summarized == {"n": 3, "f": 1.5, "t": True, "none": None}


def test_summarize_args_caps_non_string_containers() -> None:
    summarized = summarize_args({"a": [[[1]]]})

    assert summarized == {"a": [["…"]]}


def test_summarize_args_redacts_unknown_values_as_strings() -> None:
    summarized = summarize_args({"path": Path("/tmp/secret")})

    assert summarized == {"path": "/tmp/secret"}


async def test_in_memory_audit_sink_records_entries() -> None:
    sink = InMemoryAuditSink()
    entry = AuditEntry(
        workspace_id="ws-1",
        actor="agent:junior-1",
        action="tool.read_file",
        target="README.md",
        details={"call_id": "call-1", "ok": True},
    )

    await sink.record(entry)

    assert sink.entries == [entry]


async def test_db_audit_sink_writes_in_its_own_transaction(
    database: Database,
    session: AsyncSession,
) -> None:
    workspace = await make_workspace(session, slug="audit-sink")
    await session.commit()
    sink = DbAuditSink(database.sessions)

    await sink.record(
        AuditEntry(
            workspace_id=workspace.id,
            actor="agent:junior-1",
            action="tool.write_file",
            target="a.txt",
            details={"call_id": "call-1", "ok": True, "decision": "allow"},
        )
    )

    async with database.sessions() as check_session:
        rows = await AuditLogRepo(check_session, AuditLog, workspace.id).list()
    assert len(rows) == 1
    assert rows[0].action == "tool.write_file"
    assert rows[0].target == "a.txt"
    assert rows[0].details_json == {
        "call_id": "call-1",
        "ok": True,
        "decision": "allow",
    }


async def test_db_audit_sink_swallows_a_failing_write(
    database: Database,
    session: AsyncSession,
    monkeypatch,
) -> None:
    workspace = await make_workspace(session, slug="audit-sink-failure")
    await session.commit()
    sink = DbAuditSink(database.sessions)

    async def boom(self, **kwargs):
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(AuditLogRepo, "append", boom)

    await sink.record(
        AuditEntry(
            workspace_id=workspace.id,
            actor="agent:junior-1",
            action="tool.read_file",
            target="a.txt",
            details={},
        )
    )


def _sha256(value: str) -> str:
    import hashlib

    return hashlib.sha256(value.encode("utf-8")).hexdigest()
