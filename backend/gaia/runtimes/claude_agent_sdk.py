"""The Claude Agent SDK runtime (doc 10c).

This is the *thin adapter* ADR-001 accepted for lead implementation work.  The
SDK supplies only the agent loop and the model; tools, policy, approvals,
audit, events, environment, sandbox, state and cost all stay Gaia's.
"""

# Banned SDK state/cost names (ADR-001 P4).  A test greps this file and fails
# if any of these names appears outside a comment:
# resume, continue_conversation, fork_session, enable_file_checkpointing,
# total_cost_usd, model_usage.

from __future__ import annotations

import asyncio
import os
import shlex
import shutil
import tempfile
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

import claude_agent_sdk
import structlog
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from gaia.config.settings import Settings
from gaia.db.engine import session_scope
from gaia.db.ids import uuid7
from gaia.db.models import Run
from gaia.db.repo import ScopedRepo
from gaia.domain.enums import AgentRole, EventType
from gaia.llm.base import LLMUsage
from gaia.llm.run_tokens import MintedToken, mint, revoke
from gaia.runtimes.base import AgentResult, AgentTask, RuntimeUsage
from gaia.runtimes.finish import (
    FINISH_TOOL_DESCRIPTION,
    FINISH_TOOL_NAME,
    FinishArgs,
    finish_input_schema,
)
from gaia.runtimes.orphans import OrphanRegistry
from gaia.security.redaction import redact
from gaia.security.sandbox import SandboxSpec, find_srt, write_srt_settings
from gaia.services.costs import PriceTable, load_price_table, remaining_budget
from gaia.services.events import EventSink
from gaia.tools.registry import ToolContext, ToolRegistry
from gaia.tools.shell import MAX_COMMAND_TIMEOUT_S, sandbox_settings_dir

logger = structlog.get_logger(__name__)

#: The SDK's bundled CLI.  ``claude_agent_sdk`` is only imported in this module
#: (ADR-001 P1), so this lookup is the one place that knows where the binary is.
_DEFAULT_MODEL = "claude-opus-5-5"

#: Built-in Claude Code tools that must never be offered (doc 10c, P3).
_DISALLOWED_TOOLS = (
    "Bash",
    "Read",
    "Write",
    "Edit",
    "MultiEdit",
    "NotebookEdit",
    "Glob",
    "Grep",
    "LS",
    "WebFetch",
    "WebSearch",
    "Task",
    "TodoWrite",
    "Skill",
    "KillShell",
    "BashOutput",
)

#: The SDK result subtype the bundled CLI uses when ``max_turns`` is reached.
#: The Python SDK does not define a constant for it; ``error_max_turns`` is the
#: wire value used by Claude Code, and ``terminal_reason`` may be ``max_turns``.
_MAX_TURNS_SUBTYPE = "error_max_turns"
_MAX_TURNS_TERMINAL_REASON = "max_turns"

#: Maximum length of an ``AgentResult.error`` string, mirrored from support.py.
_MAX_ERROR_CHARS = 500

#: Seconds to wait for the wrapper's pid file after ``connect()``.
_PID_FILE_TIMEOUT_S = 2.0

#: The CLI's per-tool-call timeout (``MCP_TOOL_TIMEOUT``, verified live to be
#: honoured) must cover the worst case of one Gaia tool call: an in-turn approval
#: wait **and then** the longest command ``run_command`` allows, plus this
#: padding (lead review C3). A shorter value makes the CLI abandon a call that
#: Gaia is still executing.
_MCP_TOOL_TIMEOUT_PADDING_S = 120


class ClientLike(Protocol):
    """The tiny slice of :class:`claude_agent_sdk.ClaudeSDKClient` Gaia uses.

    Kept in this module so tests can inject a fake without importing the SDK
    elsewhere (ADR-001 P1).
    """

    def __aenter__(self) -> Any: ...

    def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> Any: ...

    async def query(self, prompt: str, session_id: str = "default") -> None: ...

    def receive_response(self) -> AsyncIterator[Any]: ...

    async def interrupt(self) -> None: ...


ClientFactory = Callable[[claude_agent_sdk.ClaudeAgentOptions], ClientLike]
ContextFactory = Callable[[AgentTask, EventSink, Path], Awaitable[ToolContext]]
BudgetExhausted = Callable[[AgentTask], Awaitable[bool]]
TokenMint = Callable[[AgentTask], Awaitable[MintedToken]]
TokenRevoke = Callable[[AgentTask, str], Awaitable[None]]


def build_wrapper_script(
    *,
    srt_path: Path,
    settings_path: Path,
    cli_binary: Path,
    pid_file: Path,
    agent_path: str,
    task_dir: Path,
    approval_timeout_s: float,
) -> str:
    """Return the per-task ``cli_path`` wrapper script text.

    The SDK merges its ``env`` option onto Gaia's own ``os.environ``, which may
    hold real provider keys in memory.  The wrapper therefore rebuilds the
    environment from scratch with ``env -i`` before entering ``srt`` and then
    the bundled CLI.  The run token is never written into the script: it is
    passed as ``GAIA_RUN_TOKEN`` and renamed by the wrapper.
    """
    path_values = (str(srt_path), str(settings_path), str(cli_binary), str(pid_file))
    for value in path_values:
        _reject_newline_or_nul(value)
    _reject_newline_or_nul(agent_path)
    _reject_newline_or_nul(str(task_dir))

    timeout_ms = int(
        (approval_timeout_s + MAX_COMMAND_TIMEOUT_S + _MCP_TOOL_TIMEOUT_PADDING_S) * 1000
    )
    quote = shlex.quote
    return (
        "#!/bin/sh\n"
        "# Written by Gaia (doc 10c). Rebuilds the CLI's environment from scratch"
        " and enters srt.\n"
        f"printf '%s\\n' \"$$\" > {quote(str(pid_file))}\n"
        "exec /usr/bin/env -i \\\n"
        f"  PATH={quote(agent_path)} HOME={quote(str(task_dir))} "
        f"TMPDIR={quote(str(task_dir))} LANG=C.UTF-8 \\\n"
        '  ANTHROPIC_BASE_URL="$GAIA_ANTHROPIC_BASE_URL" '
        'ANTHROPIC_API_KEY="$GAIA_RUN_TOKEN" \\\n'
        "  CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1 DISABLE_AUTOUPDATER=1 \\\n"
        f"  MCP_TOOL_TIMEOUT={quote(str(timeout_ms))} \\\n"
        '  CLAUDE_CODE_ENTRYPOINT="$CLAUDE_CODE_ENTRYPOINT" '
        'CLAUDE_AGENT_SDK_VERSION="$CLAUDE_AGENT_SDK_VERSION" \\\n'
        f"  {quote(str(srt_path))} --settings {quote(str(settings_path))} -- "
        f'/usr/bin/env -u NO_PROXY -u no_proxy {quote(str(cli_binary))} "$@"\n'
    )


def _reject_newline_or_nul(value: str) -> None:
    """Refuse a path that cannot be represented safely in the shell script."""
    if "\n" in value or "\0" in value:
        raise ValueError(f"wrapper path must not contain a newline or NUL: {value!r}")


def _prompt_path(role: AgentRole) -> Path:
    """Return the Gaia-owned system prompt for ``role``."""
    name = "lead_system.md" if role is AgentRole.LEAD else "junior_system.md"
    return Path(__file__).with_name("prompts") / name


def _load_prompt(role: AgentRole) -> str:
    """Read the Gaia-owned system prompt (ADR-001 P3: never a vendor preset)."""
    return _prompt_path(role).read_text(encoding="utf-8")


class ClaudeAgentSdkRuntime:
    """Run one task through the Claude Agent SDK with Gaia's tools only."""

    name = "claude_agent_sdk"

    def __init__(
        self,
        *,
        registry: ToolRegistry,
        name: str = "claude_agent_sdk",
        settings: Settings,
        context_factory: ContextFactory,
        sessions: async_sessionmaker[AsyncSession] | None = None,
        model: str = _DEFAULT_MODEL,
        client_factory: ClientFactory = claude_agent_sdk.ClaudeSDKClient,
        price_table: PriceTable | None = None,
        orphans: OrphanRegistry | None = None,
        approval_timeout_s: float = 600,
        budget_exhausted: BudgetExhausted | None = None,
        mint_token: TokenMint | None = None,
        revoke_token: TokenRevoke | None = None,
    ) -> None:
        self.name = name
        self._registry = registry
        self._settings = settings
        self._sessions = sessions
        self._context_factory = context_factory
        self._model = model
        self._client_factory = client_factory
        self._price_table = price_table if price_table is not None else load_price_table()
        self._orphans = orphans
        self._approval_timeout_s = approval_timeout_s
        self._budget_exhausted = budget_exhausted or self._budget_exhausted_default
        self._mint_token = mint_token or self._mint_token_default
        self._revoke_token = revoke_token or self._revoke_token_default

        self._clients: dict[str, ClientLike] = {}
        self._run_tasks: dict[str, asyncio.Task[Any]] = {}
        self._cancel_requested: set[str] = set()
        self._finish_args: dict[str, FinishArgs] = {}
        self._sdk_tools: dict[str, dict[str, claude_agent_sdk.SdkMcpTool[Any]]] = {}
        self._active_task_id: str | None = None
        self._active_pid_file: Path | None = None

    @property
    def active_task_id(self) -> str | None:
        """The task currently being prepared, used by the test SDK fake."""
        return self._active_task_id

    @property
    def active_pid_file(self) -> Path | None:
        """The wrapper pid file being prepared, used by the test SDK fake."""
        return self._active_pid_file

    def sdk_tool(self, task_id: str, name: str) -> claude_agent_sdk.SdkMcpTool[Any]:
        """Return one in-process SDK tool for ``task_id`` (used by the test fake)."""
        try:
            return self._sdk_tools[task_id][name]
        except KeyError:
            raise LookupError(f"no SDK tool {name!r} for task {task_id!r}") from None

    async def run_task(self, task: AgentTask, events: EventSink) -> AgentResult:
        """Run one task and return its result.

        The SDK session runs in its **own child task** (lead review C1, the same
        bug as 10b's N1). ``cancel()`` interrupts the client and cancels only
        that child, and ``run_task`` returns ``cancelled``. Cancelling the
        *caller* propagates: asyncio cancels the awaited child, whose
        ``finally`` disconnects, revokes the token and removes the task dir.
        """
        inner = asyncio.create_task(self._run(task, events), name=f"claude-sdk:{task.task_id}")
        self._run_tasks[task.task_id] = inner
        try:
            return await inner
        except asyncio.CancelledError:
            current = asyncio.current_task()
            if current is not None and current.cancelling():
                raise
            return _cancelled_result()
        finally:
            self._run_tasks.pop(task.task_id, None)
            self._cancel_requested.discard(task.task_id)

    async def _run(self, task: AgentTask, events: EventSink) -> AgentResult:
        task_id = task.task_id
        self._cancel_requested.discard(task_id)
        self._finish_args.pop(task_id, None)
        self._sdk_tools.pop(task_id, None)

        task_dir = Path(tempfile.mkdtemp(prefix="gaia-sdk-"))
        os.chmod(task_dir, 0o700)
        # The wrapper, the srt settings and the pid file configure the sandbox,
        # so they live where the sandboxed CLI cannot write (lead review C2, the
        # 09b S1 rule): ``task_dir`` is the CLI's HOME and TMPDIR.
        control_dir = sandbox_settings_dir(self._settings)
        token_id: str | None = None
        pid: int | None = None

        try:
            minted = await self._mint_token(task)
            token_id = minted.token_id
            token_plain = minted.token.get_secret_value()

            srt_path = find_srt(self._settings)
            settings_path = write_srt_settings(
                SandboxSpec(
                    worktree=task.worktree.work_tree,
                    tmp_dir=task_dir,
                    data_dir=self._settings.data_dir,
                    toolchain_read=self._settings.sandbox_toolchain_read,
                    gateway=f"{self._settings.host}:{self._settings.port}",
                    allowed_domains=(),
                ),
                control_dir,
            )
            wrapper_path = control_dir / "claude-wrapper"
            pid_file = control_dir / "claude.pid"
            wrapper = build_wrapper_script(
                srt_path=srt_path,
                settings_path=settings_path,
                cli_binary=_bundled_cli(),
                pid_file=pid_file,
                agent_path=os.pathsep.join(self._settings.agent_path),
                task_dir=task_dir,
                approval_timeout_s=self._approval_timeout_s,
            )
            wrapper_path.write_text(wrapper, encoding="utf-8")
            wrapper_path.chmod(0o700)

            ctx = await self._context_factory(task, events, task_dir)
            mcp_server, tool_map = self._build_mcp_server(ctx, task)
            self._sdk_tools[task_id] = tool_map

            base_url = f"http://{self._settings.host}:{self._settings.port}/llm/anthropic"
            options = self._build_options(
                task=task,
                wrapper_path=wrapper_path,
                mcp_server=mcp_server,
                env={
                    "GAIA_RUN_TOKEN": token_plain,
                    "GAIA_ANTHROPIC_BASE_URL": base_url,
                },
            )

            self._active_task_id = task_id
            self._active_pid_file = pid_file
            try:
                client = self._client_factory(options)
            finally:
                self._active_task_id = None
                self._active_pid_file = None
            self._clients[task_id] = client

            async with client as connected:
                pid = await _read_pid_file(pid_file)
                if self._orphans is not None and pid is not None:
                    self._orphans.add(
                        pid=pid,
                        task_id=task_id,
                        started_at=time.time(),
                        argv0="srt",
                    )
                await connected.query(task.instructions)
                result_message = await self._drain_messages(connected, events)

            if task_id in self._cancel_requested:
                return _cancelled_result()
            if result_message is None:
                return await self._failed_or_budget_exceeded(task, None)
            return await self._result_from_message(task, result_message)
        finally:
            if self._orphans is not None and pid is not None:
                self._orphans.remove(pid)
            self._clients.pop(task_id, None)
            self._finish_args.pop(task_id, None)
            self._sdk_tools.pop(task_id, None)
            if token_id is not None:
                await _best_effort(self._revoke_token(task, token_id))
            shutil.rmtree(task_dir, ignore_errors=True)
            shutil.rmtree(control_dir, ignore_errors=True)

    async def cancel(self, task_id: str) -> None:
        """Interrupt the live client, then cancel the asyncio task."""
        self._cancel_requested.add(task_id)
        client = self._clients.get(task_id)
        if client is not None:
            try:
                async with asyncio.timeout(5):
                    await client.interrupt()
            except TimeoutError:
                logger.warning("sdk.cancel_interrupt_timed_out", task_id=task_id)
            except Exception:
                logger.exception("sdk.cancel_interrupt_failed", task_id=task_id)

        inner = self._run_tasks.get(task_id)
        if inner is not None:
            inner.cancel()

    async def _drain_messages(
        self, client: ClientLike, events: EventSink
    ) -> claude_agent_sdk.ResultMessage | None:
        """Consume the SDK stream and emit Gaia events, returning the result."""
        result_message: claude_agent_sdk.ResultMessage | None = None
        async for message in client.receive_response():
            if isinstance(message, claude_agent_sdk.AssistantMessage):
                for block in message.content:
                    if isinstance(block, claude_agent_sdk.TextBlock):
                        await events.emit(
                            EventType.AGENT_MESSAGE,
                            {"content": block.text},
                        )
            elif isinstance(message, claude_agent_sdk.ResultMessage):
                result_message = message
                break
        return result_message

    def _build_options(
        self,
        *,
        task: AgentTask,
        wrapper_path: Path,
        mcp_server: Any,
        env: dict[str, str],
    ) -> claude_agent_sdk.ClaudeAgentOptions:
        """Build SDK options with built-ins off and only Gaia MCP tools allowed."""
        tool_names = (*self._registry.names(), FINISH_TOOL_NAME)
        allowed_tools = [f"mcp__gaia__{name}" for name in tool_names]
        return claude_agent_sdk.ClaudeAgentOptions(
            tools=[],
            disallowed_tools=list(_DISALLOWED_TOOLS),
            mcp_servers={"gaia": mcp_server},
            strict_mcp_config=True,
            permission_mode="dontAsk",
            allowed_tools=allowed_tools,
            hooks={
                "PreToolUse": [
                    claude_agent_sdk.HookMatcher(
                        matcher=None,
                        hooks=[self._pre_tool_use],
                    )
                ]
            },
            setting_sources=[],
            system_prompt=_load_prompt(task.role),
            max_turns=task.max_turns,
            model=self._model,
            cwd=str(task.worktree.work_tree),
            cli_path=str(wrapper_path),
            env=env,
        )

    def _build_mcp_server(
        self, ctx: ToolContext, task: AgentTask
    ) -> tuple[Any, dict[str, claude_agent_sdk.SdkMcpTool[Any]]]:
        """Build one in-process ``gaia`` MCP server over the task registry."""
        tools: list[claude_agent_sdk.SdkMcpTool[Any]] = []
        tool_map: dict[str, claude_agent_sdk.SdkMcpTool[Any]] = {}

        for spec in self._registry.specs():
            name = spec.name

            async def handler(args: Any, _name: str = name) -> dict[str, Any]:
                result = await self._registry.call(_name, args, ctx, call_id=uuid7())
                return {
                    "content": [{"type": "text", "text": result.content}],
                    "is_error": not result.ok,
                }

            sdk_tool = claude_agent_sdk.tool(name, spec.description, spec.input_schema)(handler)
            tools.append(sdk_tool)
            tool_map[name] = sdk_tool

        async def finish_handler(args: Any) -> dict[str, Any]:
            return await self._finish_tool(task, args)

        finish_tool = claude_agent_sdk.tool(
            FINISH_TOOL_NAME,
            FINISH_TOOL_DESCRIPTION,
            finish_input_schema(),
        )(finish_handler)
        tools.append(finish_tool)
        tool_map[FINISH_TOOL_NAME] = finish_tool

        server = claude_agent_sdk.create_sdk_mcp_server(
            name="gaia",
            version="1",
            tools=tools,
        )
        return server, tool_map

    async def _pre_tool_use(self, input_data: Any, tool_use_id: str | None, context: Any) -> Any:
        """Deny any tool outside ``mcp__gaia__*`` (belt and braces)."""
        del tool_use_id, context
        tool_name = str(input_data.get("tool_name", ""))
        if tool_name.startswith("mcp__gaia__"):
            return {}
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": "only Gaia tools are available",
            }
        }

    async def _finish_tool(self, task: AgentTask, args: Any) -> dict[str, Any]:
        """Validate and store the runtime's per-task ``finish`` arguments."""
        try:
            finish = FinishArgs.model_validate(args)
        except ValidationError as exc:
            return {
                "content": [{"type": "text", "text": _format_validation_error(exc)}],
                "is_error": True,
            }
        self._finish_args[task.task_id] = finish
        return {"content": [{"type": "text", "text": "finished"}], "is_error": False}

    async def _result_from_message(
        self, task: AgentTask, message: claude_agent_sdk.ResultMessage
    ) -> AgentResult:
        """Map a terminal SDK result to a Gaia result."""
        finish = self._finish_args.get(task.task_id)
        usage = self._usage_from_message(message)

        if finish is not None and not message.is_error:
            return AgentResult(
                status="completed",
                summary=finish.summary,
                structured_output=finish.model_dump(mode="json"),
                files_changed=(),
                usage=usage,
                error=None,
                transcript_ref=None,
            )

        if _is_max_turns(message):
            return AgentResult(
                status="failed",
                summary="max turns reached",
                structured_output=None,
                files_changed=(),
                usage=usage,
                error="max turns reached",
                transcript_ref=None,
            )

        return await self._failed_or_budget_exceeded(task, message)

    async def _failed_or_budget_exceeded(
        self,
        task: AgentTask,
        message: claude_agent_sdk.ResultMessage | None,
    ) -> AgentResult:
        """Return the budget/failed result for a missing or error result."""
        usage = self._usage_from_message(message) if message is not None else RuntimeUsage()
        if await self._budget_exhausted(task):
            return AgentResult(
                status="budget_exceeded",
                summary="LLM budget exceeded",
                structured_output=None,
                files_changed=(),
                usage=usage,
                error="LLM budget exceeded",
                transcript_ref=None,
            )
        return AgentResult(
            status="failed",
            summary="task failed",
            structured_output=None,
            files_changed=(),
            usage=usage,
            error=_short_result_error(message),
            transcript_ref=None,
        )

    def _usage_from_message(self, message: claude_agent_sdk.ResultMessage) -> RuntimeUsage:
        """Build Gaia usage from the SDK's token counts, priced by Gaia."""
        raw = message.usage or {}
        input_tokens = _usage_int(raw, "input_tokens")
        output_tokens = _usage_int(raw, "output_tokens")
        cache_read_tokens = _usage_int(raw, "cache_read_input_tokens")
        cache_creation_tokens = _usage_int(raw, "cache_creation_input_tokens")

        usage = LLMUsage(
            provider="anthropic",
            model=self._model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_read_tokens=cache_read_tokens,
            cache_creation_tokens=cache_creation_tokens,
        )
        cost_usd = self._price_table.cost(usage, at=datetime.now(UTC))
        return RuntimeUsage(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_tokens=cache_read_tokens + cache_creation_tokens,
            cost_usd=cost_usd,
        )

    async def _mint_token_default(self, task: AgentTask) -> MintedToken:
        """Mint a per-task Anthropic run token in its own transaction."""
        if self._sessions is None:
            raise ValueError("sessions are required for real run-token minting")
        async with session_scope(self._sessions) as session:
            return await mint(
                session,
                workspace_id=task.workspace_id,
                run_id=task.run_id,
                provider="anthropic",
                ttl_s=7200,
                run_step_id=task.run_step_id,
                agent_id=task.agent_id,
            )

    async def _revoke_token_default(self, task: AgentTask, token_id: str) -> None:
        """Revoke one task run token in its own transaction."""
        if self._sessions is None:
            raise ValueError("sessions are required for real run-token revocation")
        async with session_scope(self._sessions) as session:
            await revoke(session, task.workspace_id, token_id)

    async def _budget_exhausted_default(self, task: AgentTask) -> bool:
        """The default budget check: ask Gaia's cost service, not the SDK."""
        if self._sessions is None:
            return False
        async with session_scope(self._sessions) as session:
            run = await ScopedRepo(session, Run, task.workspace_id).get(task.run_id)
        return run is not None and remaining_budget(run) <= 0


def _bundled_cli() -> Path:
    """Return the SDK's bundled CLI, failing clearly if it is missing."""
    bundled = Path(claude_agent_sdk.__file__).parent / "_bundled" / "claude"
    if not bundled.is_file() or not os.access(bundled, os.X_OK):
        raise FileNotFoundError(
            f"bundled Claude Code CLI not found or not executable at {str(bundled)!r}"
        )
    return bundled


async def _read_pid_file(path: Path) -> int | None:
    """Read the wrapper pid file, retrying briefly while ``srt`` starts."""
    deadline = time.monotonic() + _PID_FILE_TIMEOUT_S
    while True:
        try:
            raw = await asyncio.to_thread(_read_pid_text, path)
            return int(raw.strip())
        except (FileNotFoundError, ValueError):
            if time.monotonic() >= deadline:
                logger.warning("sdk.pid_file_not_found", path=str(path))
                return None
            await asyncio.sleep(0.05)


def _read_pid_text(path: Path) -> str:
    """Blocking read for :func:`asyncio.to_thread` in :func:`_read_pid_file`."""
    return path.read_text(encoding="ascii")


async def _best_effort(awaitable: Awaitable[None]) -> None:
    """Await cleanup, logging and swallowing failures so cleanup never masks."""
    try:
        await awaitable
    except Exception:
        logger.exception("sdk.cleanup_failed")


def _usage_int(raw: Mapping[str, Any], key: str) -> int:
    """Return a non-negative integer token count from a raw usage dict."""
    try:
        value = int(raw.get(key, 0))
    except (TypeError, ValueError):
        return 0
    return max(value, 0)


def _short_result_error(message: claude_agent_sdk.ResultMessage | None) -> str:
    """A short redacted error from an SDK result, without parsing for budget."""
    if message is None:
        return "Claude Agent SDK ended without a result"
    parts: list[str] = []
    if message.errors:
        parts.extend(str(error) for error in message.errors)
    if message.result:
        parts.append(str(message.result))
    text = "; ".join(parts).strip()
    if not text:
        text = message.stop_reason or f"Claude Agent SDK result subtype {message.subtype}"
    return redact(text[:_MAX_ERROR_CHARS])


def _is_max_turns(message: claude_agent_sdk.ResultMessage) -> bool:
    """Return True when the SDK stopped because ``max_turns`` was reached."""
    return (
        message.subtype == _MAX_TURNS_SUBTYPE
        or message.terminal_reason == _MAX_TURNS_TERMINAL_REASON
    )


def _format_validation_error(exc: ValidationError) -> str:
    """Render one ``loc: message`` line per validation error, capped."""
    lines = [
        f"{'.'.join(str(part) for part in error.get('loc', ()))}: {error.get('msg', 'invalid')}"
        for error in exc.errors()
    ]
    return "\n".join(lines)[:2000]


def _cancelled_result() -> AgentResult:
    """The result returned when :meth:`ClaudeAgentSdkRuntime.cancel` wins."""
    return AgentResult(
        status="cancelled",
        summary="cancelled",
        structured_output=None,
        files_changed=(),
        usage=RuntimeUsage(),
        error=None,
        transcript_ref=None,
    )
