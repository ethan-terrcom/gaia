"""Tests for the tool registry dispatch path (doc 09b)."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, cast

import pytest
from pydantic import BaseModel

from gaia.domain.enums import EventType
from gaia.security.audit import AuditEntry, InMemoryAuditSink
from gaia.security.paths import PathDenied
from gaia.tools import registry as registry_module
from gaia.tools.registry import (
    ALL_TOOLS,
    JUNIOR_TOOLS,
    MAX_RESULT_CHARS,
    Tool,
    ToolContext,
    ToolInput,
    ToolRegistry,
    ToolResult,
    UnknownTool,
    _audit_target,
    build_default_registry,
)
from tests.tool_fakes import RecordingEventSink, make_tool_context


class DummyInput(ToolInput):
    text: str = "ok"


async def _dummy_handler(input: BaseModel, ctx: ToolContext) -> ToolResult:
    del ctx
    model = cast(DummyInput, input)
    if model.text == "raise":
        raise RuntimeError("secret detail")
    if model.text == "path":
        raise PathDenied("escape attempt")
    if model.text == "cancel":
        raise asyncio.CancelledError()
    if model.text == "long":
        return ToolResult(True, "x" * 70_000)
    return ToolResult(True, model.text, data={"echo": model.text})


@pytest.fixture
def registry() -> ToolRegistry:
    built = ToolRegistry()
    built.register(
        Tool(
            name="dummy",
            description="A dummy tool",
            input_model=DummyInput,
            handler=_dummy_handler,
            mutates=False,
        )
    )
    return built


@pytest.fixture
def ctx(tmp_path: Path) -> ToolContext:
    return make_tool_context(tmp_path / "worktree")


def test_duplicate_registration_is_refused(registry: ToolRegistry) -> None:
    duplicate = Tool(
        name="dummy",
        description="Another dummy",
        input_model=DummyInput,
        handler=_dummy_handler,
        mutates=False,
    )

    with pytest.raises(ValueError, match="already registered"):
        registry.register(duplicate)


def test_get_unknown_tool_raises(registry: ToolRegistry) -> None:
    with pytest.raises(UnknownTool):
        registry.get("missing")


def test_subset_builds_a_smaller_registry(registry: ToolRegistry) -> None:
    registry.register(
        Tool(
            name="other",
            description="Another tool",
            input_model=DummyInput,
            handler=_dummy_handler,
            mutates=False,
        )
    )

    subset = registry.subset(["dummy"])

    assert subset.names() == ("dummy",)
    with pytest.raises(UnknownTool):
        registry.subset(["missing"])


def test_specs_are_vendor_neutral_json_schema(registry: ToolRegistry) -> None:
    specs = registry.specs()

    assert len(specs) == 1
    assert specs[0].name == "dummy"
    assert specs[0].description == "A dummy tool"
    assert specs[0].input_schema["type"] == "object"
    assert specs[0].input_schema["additionalProperties"] is False
    assert "text" in specs[0].input_schema["properties"]


async def test_unknown_tool_returns_a_result_and_audits(
    registry: ToolRegistry, ctx: ToolContext
) -> None:
    result = await registry.call("missing", {}, ctx, call_id="call-unknown")

    events = cast(RecordingEventSink, ctx.events)
    audit = cast(InMemoryAuditSink, ctx.audit)
    assert result.ok is False
    assert result.content == "unknown tool: missing"
    assert result.decision == "deny"
    assert [event[0] for event in events.events] == [
        EventType.AGENT_TOOL_CALL,
        EventType.AGENT_TOOL_RESULT,
    ]
    assert audit.entries[0].action == "tool.missing"
    assert audit.entries[0].details["call_id"] == "call-unknown"


async def test_invalid_arguments_reject_extra_fields(
    registry: ToolRegistry, ctx: ToolContext
) -> None:
    result = await registry.call("dummy", {"text": "ok", "extra": 1}, ctx, call_id="call-extra")

    assert result.ok is False
    assert "extra: Extra inputs are not permitted" in result.content
    assert result.decision == "deny"


async def test_handler_exception_is_logged_but_not_leaked(
    registry: ToolRegistry,
    ctx: ToolContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class CapturingLogger:
        def __init__(self) -> None:
            self.events: list[tuple[str, dict[str, Any]]] = []

        def exception(self, event: str, **kwargs: Any) -> None:
            self.events.append((event, kwargs))

    logger = CapturingLogger()
    monkeypatch.setattr(registry_module, "logger", logger)

    result = await registry.call("dummy", {"text": "raise"}, ctx, call_id="call-raise")

    assert result.ok is False
    assert result.content == "tool failed: RuntimeError"
    assert "secret detail" not in result.content
    assert ("tool.failed", {"tool": "dummy", "call_id": "call-raise"}) in logger.events


async def test_path_denied_becomes_a_denied_result(
    registry: ToolRegistry, ctx: ToolContext
) -> None:
    result = await registry.call("dummy", {"text": "path"}, ctx, call_id="call-path")

    assert result.ok is False
    assert result.content == "path denied: escape attempt"
    assert result.decision == "deny"


async def test_failed_call_still_audits_and_emits_both_events(
    registry: ToolRegistry, ctx: ToolContext
) -> None:
    events = cast(RecordingEventSink, ctx.events)
    audit = cast(InMemoryAuditSink, ctx.audit)
    result = await registry.call("dummy", {"text": "raise"}, ctx, call_id="call-failed")

    assert result.ok is False
    assert len(audit.entries) == 1
    assert audit.entries[0].details["ok"] is False
    assert audit.entries[0].details["call_id"] == "call-failed"
    assert [event[0] for event in events.events] == [
        EventType.AGENT_TOOL_CALL,
        EventType.AGENT_TOOL_RESULT,
    ]


async def test_cancelled_call_still_audits_and_emits_both_events(
    registry: ToolRegistry, ctx: ToolContext
) -> None:
    events = cast(RecordingEventSink, ctx.events)
    audit = cast(InMemoryAuditSink, ctx.audit)
    with pytest.raises(asyncio.CancelledError):
        await registry.call("dummy", {"text": "cancel"}, ctx, call_id="call-cancel")

    assert len(audit.entries) == 1
    assert audit.entries[0].details["decision"] == "deny"
    assert [event[0] for event in events.events] == [
        EventType.AGENT_TOOL_CALL,
        EventType.AGENT_TOOL_RESULT,
    ]


async def test_result_content_is_capped_by_the_registry(
    registry: ToolRegistry, ctx: ToolContext
) -> None:
    events = cast(RecordingEventSink, ctx.events)
    result = await registry.call("dummy", {"text": "long"}, ctx, call_id="call-long")

    assert len(result.content) > MAX_RESULT_CHARS
    assert result.content.endswith("\n[truncated: 6000 more characters]")
    result_event = events.events[-1][1]
    assert result_event["content"] == "x" * 2_000


def test_invalid_tool_name_is_refused() -> None:
    with pytest.raises(ValueError, match="tool name"):
        Tool(
            name="BadName",
            description="bad",
            input_model=DummyInput,
            handler=_dummy_handler,
            mutates=False,
        )


def test_build_default_registry_lists_all_junior_tools() -> None:
    registry = build_default_registry(search_supports_regex=True)

    assert registry.names() == ALL_TOOLS
    assert JUNIOR_TOOLS == tuple(name for name in ALL_TOOLS if name != "git_commit")
    assert "git_commit" not in JUNIOR_TOOLS
    assert len(registry.specs()) == len(ALL_TOOLS)


def test_audit_target_for_commands_and_default_paths() -> None:
    assert _audit_target("run_command", {"argv": ["pytest", "-q"]}) == "pytest -q"
    assert _audit_target("run_command", {"argv": "pytest"}) is None
    assert _audit_target("list_dir", {}) == "."
    assert _audit_target("search", {}) == "."
    assert _audit_target("read_file", {}) is None
    assert _audit_target("read_file", {"path": ""}) is None
    assert _audit_target("read_file", {"path": "a.txt"}) == "a.txt"
    assert _audit_target("git_diff", {"path": "a.txt"}) == "a.txt"


def test_tool_context_root_is_the_worktree(tmp_path: Path) -> None:
    ctx = make_tool_context(tmp_path / "worktree")

    assert ctx.root == (tmp_path / "worktree").resolve()


async def test_approve_calls_the_broker_with_tool_and_reason(tmp_path: Path) -> None:
    class _Broker:
        def __init__(self) -> None:
            self.payload: dict[str, Any] | None = None

        async def request(self, task: Any, *, kind: Any, payload: Any, timeout_s: float):
            del task, kind, timeout_s
            self.payload = dict(payload)
            return None

    broker = _Broker()
    ctx = make_tool_context(tmp_path / "worktree", approvals=broker)

    await registry_module.approve(ctx, {"path": "a.txt"}, "write it", tool="write_file")

    assert broker.payload == {
        "path": "a.txt",
        "tool": "write_file",
        "reason": "write it",
    }


class _RaisingEventSink:
    async def emit(self, type, payload):
        raise RuntimeError("event bus down")


class _RaisingAuditSink:
    async def record(self, entry: AuditEntry) -> None:
        raise RuntimeError("audit db down")


async def test_event_and_audit_failures_do_not_break_a_tool_call(
    tmp_path: Path,
) -> None:
    events = _RaisingEventSink()
    audit = _RaisingAuditSink()
    ctx = make_tool_context(tmp_path / "worktree", events=events, audit=audit)
    registry = ToolRegistry()
    registry.register(
        Tool(
            name="dummy",
            description="A dummy tool",
            input_model=DummyInput,
            handler=_dummy_handler,
            mutates=False,
        )
    )

    result = await registry.call("dummy", {"text": "ok"}, ctx, call_id="call-resilient")

    assert result.ok is True
    assert result.content == "ok"


class _AwaitingAuditSink:
    """An audit sink that yields to the event loop, like the real DB sink does."""

    def __init__(self) -> None:
        self.entries: list[AuditEntry] = []

    async def record(self, entry: AuditEntry) -> None:
        await asyncio.sleep(0)
        self.entries.append(entry)


class _AwaitingEventSink(RecordingEventSink):
    async def emit(self, type: EventType, payload: Any) -> None:
        await asyncio.sleep(0)
        await super().emit(type, payload)


async def test_audit_survives_a_level_triggered_cancel_scope(tmp_path: Path) -> None:
    """Lead review (09b R1): the Agent SDK runs tool handlers inside an anyio task
    group. anyio cancellation is level-triggered, so every await in the registry's
    ``finally`` would be cancelled too unless it is shielded, and the audit row and
    the result event would be lost."""
    import anyio

    started = asyncio.Event()

    async def _blocking_handler(input: BaseModel, ctx: ToolContext) -> ToolResult:
        del input, ctx
        started.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    blocking = ToolRegistry()
    blocking.register(
        Tool(
            name="blocks",
            description="blocks forever",
            input_model=DummyInput,
            handler=_blocking_handler,
            mutates=False,
        )
    )
    audit = _AwaitingAuditSink()
    events = _AwaitingEventSink()
    ctx = make_tool_context(tmp_path / "worktree", audit=audit, events=events)

    async with anyio.create_task_group() as group:

        async def _run() -> None:
            await blocking.call("blocks", {}, ctx, call_id="call-scope")

        group.start_soon(_run)
        await started.wait()
        group.cancel_scope.cancel()

    assert len(audit.entries) == 1
    assert audit.entries[0].details["call_id"] == "call-scope"
    assert [event[0] for event in events.events] == [
        EventType.AGENT_TOOL_CALL,
        EventType.AGENT_TOOL_RESULT,
    ]


async def test_unusual_arguments_never_escape_the_finally(
    registry: ToolRegistry, ctx: ToolContext
) -> None:
    """Lead review (09b R2): building the audit details must not raise out of ``finally``."""
    result = await registry.call("dummy", cast(Any, ["not", "a", "mapping"]), ctx, call_id="c")
    assert result.ok is False
