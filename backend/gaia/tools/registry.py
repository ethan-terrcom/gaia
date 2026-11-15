"""The one dispatch path every agent runtime uses for tools (doc 09b).

A runtime builds a :class:`ToolContext` per task and then calls
:meth:`ToolRegistry.call` for every tool invocation the model requests. The
registry is the only place that validates arguments, runs handlers, caps
content, writes audit rows and emits the ``agent.tool_call`` /
``agent.tool_result`` event pair, so policy, approvals, audit and events are
identical whichever runtime or vendor is calling (ADR-001 P3).

The registry deliberately knows nothing about a model vendor. Its output shape
is the neutral :class:`~gaia.llm.base.LLMTool`, whose JSON Schema is the only
tool description a runtime hands to a model.
"""

from __future__ import annotations

import asyncio
import re
import shlex
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

import anyio
import structlog
from pydantic import BaseModel, ConfigDict, ValidationError

from gaia.domain.enums import ApprovalKind, EventType
from gaia.llm.base import LLMTool
from gaia.runtimes.approvals import ApprovalBroker, ApprovalDecision
from gaia.runtimes.base import AgentTask
from gaia.security.audit import AuditEntry, AuditSink, summarize_args
from gaia.security.commands import CommandPolicy
from gaia.security.paths import PathDenied, UnsafePath
from gaia.services.events import EventSink
from gaia.services.git import GitService

if TYPE_CHECKING:
    from gaia.tools.shell import CommandRunner

logger = structlog.get_logger(__name__)

#: The registry caps every result string at this many characters.
MAX_RESULT_CHARS = 64_000

#: Event payloads carry only this much of a result's content; the bus truncates
#: again at 8 KB, but this keeps a 64 KB result from being copied into an event.
_EVENT_CONTENT_CHARS = 2_000

#: Validation error text handed to a model is capped as a whole.
_MAX_VALIDATION_CHARS = 2_000

#: Longest the audit write and result event may take once a call is over. They
#: run shielded from cancellation (see :meth:`ToolRegistry.call`), so they need
#: their own bound.
_FINALIZE_TIMEOUT_S = 10.0

#: ``Tool.name`` must be this shape.
_TOOL_NAME = re.compile(r"^[a-z][a-z0-9_]{1,40}$")

ToolDecision = Literal["allow", "deny", "ask_approved", "ask_rejected"]


class ToolInput(BaseModel):
    """Base for every tool input model.

    ``extra="forbid"`` is the fail-closed rule for model arguments: a field the
    tool does not define must be rejected rather than silently ignored.
    """

    model_config = ConfigDict(extra="forbid")


@dataclass(frozen=True)
class ToolResult:
    """What one tool call returns to the model and to the audit trail.

    ``content`` is the only text a model sees; the registry caps it at
    :data:`MAX_RESULT_CHARS`. ``data`` is structured detail for events and tests
    and never goes to a model verbatim.
    """

    ok: bool
    content: str
    data: Mapping[str, Any] | None = None
    decision: ToolDecision = "allow"


@dataclass(frozen=True)
class ToolContext:
    """Everything one task supplies to a tool handler.

    A runtime builds one per task. ``task_dir`` is the per-task scratch dir the
    runtime creates; it is the sandbox ``tmp_dir`` and the command's
    ``HOME``/``TMPDIR``.
    """

    task: AgentTask
    git: GitService
    policy: CommandPolicy
    approvals: ApprovalBroker
    audit: AuditSink
    events: EventSink
    commands: CommandRunner
    task_dir: Path
    actor: str
    approval_timeout_s: float = 600
    rg_path: Path | None = None

    @property
    def root(self) -> Path:
        """The worktree root: every agent path is confined under this."""
        return self.task.worktree.work_tree


@dataclass(frozen=True)
class Tool:
    """One registered tool: name, model input, and an async handler."""

    name: str
    description: str
    input_model: type[BaseModel]
    handler: Callable[[BaseModel, ToolContext], Awaitable[ToolResult]]
    mutates: bool

    def __post_init__(self) -> None:
        if _TOOL_NAME.fullmatch(self.name) is None:
            raise ValueError(f"tool name {self.name!r} must match {_TOOL_NAME.pattern}")


class UnknownTool(KeyError):
    """The registry does not know a tool name."""

    def __init__(self, name: str) -> None:
        self.name = name
        super().__init__(name)


class ToolRegistry:
    """A per-process lookup of tools by name."""

    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}
        self._order: list[str] = []

    def register(self, tool: Tool) -> None:
        """Register ``tool`` under its name, refusing duplicates."""
        if tool.name in self._tools:
            raise ValueError(f"tool {tool.name!r} is already registered")
        self._tools[tool.name] = tool
        self._order.append(tool.name)

    def get(self, name: str) -> Tool:
        """Return the tool registered as ``name``."""
        try:
            return self._tools[name]
        except KeyError:
            raise UnknownTool(name) from None

    def names(self) -> tuple[str, ...]:
        """The registered tool names, in registration order."""
        return tuple(self._order)

    def subset(self, names: tuple[str, ...] | list[str]) -> ToolRegistry:
        """Build a new registry with just ``names``, in the given order."""
        registry = ToolRegistry()
        for name in names:
            registry.register(self.get(name))
        return registry

    def specs(self) -> tuple[LLMTool, ...]:
        """The vendor-neutral JSON Schema descriptions of every tool."""
        return tuple(
            LLMTool(
                name=tool.name,
                description=tool.description,
                input_schema=tool.input_model.model_json_schema(),
            )
            for tool in self._ordered_tools()
        )

    async def call(
        self,
        name: str,
        arguments: Mapping[str, Any],
        ctx: ToolContext,
        *,
        call_id: str,
    ) -> ToolResult:
        """Dispatch one tool call: validate, run, cap, audit and emit.

        This method never raises for anything the model did. Unknown tools,
        invalid arguments, denied paths and handler exceptions all become a
        :class:`ToolResult` with ``ok=False``. ``asyncio.CancelledError`` is
        re-raised after the audit row and result event are written.
        """
        started = time.monotonic()
        tool = self._tools.get(name)
        result = ToolResult(False, "tool did not run", decision="deny")
        # A runtime may hand over whatever the model produced. Anything that is
        # not a mapping is refused below; the audit trail records it as empty.
        is_mapping = isinstance(arguments, Mapping)
        audit_args: Mapping[str, Any] = arguments if is_mapping else {}

        try:
            await _emit_tool_call(ctx, call_id, name, audit_args)

            if not is_mapping:
                result = ToolResult(
                    False,
                    "arguments must be a JSON object",
                    decision="deny",
                    data={"reason": "invalid arguments"},
                )
            elif tool is None:
                result = ToolResult(
                    False,
                    f"unknown tool: {name}",
                    decision="deny",
                    data={"reason": "unknown tool"},
                )
            else:
                try:
                    model = tool.input_model.model_validate(arguments)
                except ValidationError as exc:
                    result = ToolResult(
                        False,
                        _format_validation_errors(exc),
                        decision="deny",
                        data={"reason": "invalid arguments"},
                    )
                else:
                    result = await tool.handler(model, ctx)
        except asyncio.CancelledError:
            result = ToolResult(
                False,
                "tool cancelled",
                decision="deny",
                data={"reason": "cancelled"},
            )
            raise
        except (PathDenied, UnsafePath) as exc:
            reason = str(exc)
            result = ToolResult(
                False,
                f"path denied: {reason}",
                decision="deny",
                data={"reason": reason},
            )
        except Exception as exc:
            # The full exception is logged for the operator, never sent to the
            # model: exception text can echo secrets from a file or argv.
            logger.exception("tool.failed", tool=name, call_id=call_id)
            result = ToolResult(
                False,
                f"tool failed: {type(exc).__name__}",
                decision="deny",
                data={"reason": type(exc).__name__},
            )
        finally:
            duration_ms = round((time.monotonic() - started) * 1000)
            result = _truncate_result(result)
            # Shielded (lead review R1): the Agent SDK runs tool handlers inside
            # an anyio task group, where cancellation is level-triggered, so an
            # unshielded await here would be cancelled too and the audit row
            # lost. Bounded so a stuck sink cannot hold a cancelled task forever.
            with anyio.CancelScope(shield=True), anyio.move_on_after(_FINALIZE_TIMEOUT_S):
                await _write_audit(ctx, name, audit_args, result, duration_ms, call_id)
                await _emit_tool_result(ctx, call_id, name, result, duration_ms)

        return result

    def _ordered_tools(self) -> tuple[Tool, ...]:
        return tuple(self._tools[name] for name in self._order)


async def approve(
    ctx: ToolContext,
    payload: Mapping[str, Any],
    reason: str,
    *,
    tool: str,
) -> ApprovalDecision:
    """Ask the approval broker and return its decision.

    The broker (doc 10a) already upserts the row, publishes, waits inside the
    turn and treats a timeout as deny. The tool name is a parameter here, never
    something the model can smuggle in through ``payload``.
    """
    return await ctx.approvals.request(
        ctx.task,
        kind=ApprovalKind.COMMAND,
        payload={**payload, "tool": tool, "reason": reason},
        timeout_s=ctx.approval_timeout_s,
    )


def _format_validation_errors(exc: ValidationError) -> str:
    """One ``loc: message`` line per validation error, capped as a whole."""
    lines = [
        f"{'.'.join(str(part) for part in error.get('loc', ()))}: {error.get('msg', 'invalid')}"
        for error in exc.errors()
    ]
    text = "\n".join(lines)
    return text[:_MAX_VALIDATION_CHARS]


def _truncate_result(result: ToolResult) -> ToolResult:
    """Cap ``result.content`` at :data:`MAX_RESULT_CHARS` with a trailing note."""
    content = result.content
    if len(content) <= MAX_RESULT_CHARS:
        return result
    extra = len(content) - MAX_RESULT_CHARS
    return ToolResult(
        ok=result.ok,
        content=f"{content[:MAX_RESULT_CHARS]}\n[truncated: {extra} more characters]",
        data=result.data,
        decision=result.decision,
    )


async def _emit_tool_call(
    ctx: ToolContext, call_id: str, name: str, arguments: Mapping[str, Any]
) -> None:
    try:
        await ctx.events.emit(
            EventType.AGENT_TOOL_CALL,
            {"call_id": call_id, "tool": name, "args": summarize_args(arguments)},
        )
    except Exception:
        logger.exception("tool.call_event_failed", tool=name, call_id=call_id)


async def _emit_tool_result(
    ctx: ToolContext, call_id: str, name: str, result: ToolResult, duration_ms: int
) -> None:
    try:
        await ctx.events.emit(
            EventType.AGENT_TOOL_RESULT,
            {
                "call_id": call_id,
                "tool": name,
                "ok": result.ok,
                "decision": result.decision,
                "duration_ms": duration_ms,
                "content": result.content[:_EVENT_CONTENT_CHARS],
            },
        )
    except Exception:
        logger.exception("tool.result_event_failed", tool=name, call_id=call_id)


async def _write_audit(
    ctx: ToolContext,
    name: str,
    arguments: Mapping[str, Any],
    result: ToolResult,
    duration_ms: int,
    call_id: str,
) -> None:
    """Write one audit row for the call, whatever its outcome."""
    try:
        details: dict[str, Any] = {
            "task_id": ctx.task.task_id,
            "run_id": ctx.task.run_id,
            "call_id": call_id,
            "args": summarize_args(arguments),
            "ok": result.ok,
            "decision": result.decision,
            "reason": _reason_from_result(result),
            "duration_ms": duration_ms,
        }
        await ctx.audit.record(
            AuditEntry(
                workspace_id=ctx.task.workspace_id,
                actor=ctx.actor,
                action=f"tool.{name}",
                target=_audit_target(name, arguments),
                details=details,
            )
        )
    except Exception:
        logger.exception("audit.write_failed", tool=name)


def _reason_from_result(result: ToolResult) -> str:
    data = result.data
    if isinstance(data, Mapping):
        reason = data.get("reason")
        if isinstance(reason, str):
            return reason
    return ""


_PATH_TOOLS = frozenset(
    {"read_file", "list_dir", "write_file", "edit_file", "delete_file", "search"}
)


def _audit_target(name: str, arguments: Mapping[str, Any]) -> str | None:
    """A short human-readable target for the audit row."""
    if name == "run_command":
        argv = arguments.get("argv")
        if isinstance(argv, list) and all(isinstance(item, str) for item in argv):
            return shlex.join(argv)[:500] or None
        return None
    if name in _PATH_TOOLS:
        raw = arguments.get("path")
        if raw is None:
            return "." if name in {"list_dir", "search"} else None
        return str(raw)[:500] or None
    if name == "git_diff" and arguments.get("path") is not None:
        return str(arguments.get("path"))[:500]
    return None


def build_default_registry(*, search_supports_regex: bool) -> ToolRegistry:
    """Build the production registry with every tool a runtime offers."""
    from gaia.tools import fs, git_tools, shell

    registry = ToolRegistry()
    registry.register(fs.read_file)
    registry.register(fs.list_dir)
    registry.register(fs.write_file)
    registry.register(fs.edit_file)
    registry.register(fs.delete_file)
    registry.register(fs.search(search_supports_regex=search_supports_regex))
    registry.register(shell.run_command)
    registry.register(git_tools.git_status)
    registry.register(git_tools.git_diff)
    registry.register(git_tools.git_commit)
    return registry


ALL_TOOLS = (
    "read_file",
    "list_dir",
    "write_file",
    "edit_file",
    "delete_file",
    "search",
    "run_command",
    "git_status",
    "git_diff",
    "git_commit",
)

JUNIOR_TOOLS = tuple(name for name in ALL_TOOLS if name != "git_commit")
