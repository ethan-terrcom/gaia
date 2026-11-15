"""The native, provider-neutral agent tool loop (doc 10b, ADR-001).

This runtime is the default junior runtime. It knows the neutral
:class:`~gaia.llm.base.ToolChatLLM` seam, Gaia's :class:`ToolRegistry` and the
shared :class:`ToolContext`; it never imports a vendor SDK or a concrete client.
Every model call goes through an injected ``llm_factory`` (normally
:func:`gaia.runtimes.native_llm.gateway_llm_factory`) and every tool call goes
through the registry, which already owns policy, approvals, audit and events.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import ValidationError

from gaia.domain.enums import EventType
from gaia.llm.base import (
    LLMBudgetExceeded,
    LLMMessage,
    LLMResponse,
    LLMTool,
    LLMUsage,
)
from gaia.runtimes.base import AgentResult, AgentTask, RuntimeUsage
from gaia.runtimes.finish import (
    FINISH_TOOL_DESCRIPTION,
    FINISH_TOOL_NAME,
    FinishArgs,
)
from gaia.services.events import EventSink
from gaia.tools.registry import ToolContext, ToolRegistry

if TYPE_CHECKING:
    from gaia.runtimes.native_llm import LlmHandle

_PROMPTS_DIR = Path(__file__).with_name("prompts")
JUNIOR_SYSTEM_PROMPT = (_PROMPTS_DIR / "junior_system.md").read_text(encoding="utf-8")

#: ``finish`` is runtime protocol, not a registry tool: it never touches the
#: worktree, so it is built here from :class:`FinishArgs` and is the one tool
#: the registry does not own (ADR-001 P3).
FINISH_TOOL = LLMTool(
    name=FINISH_TOOL_NAME,
    description=FINISH_TOOL_DESCRIPTION,
    input_schema=FinishArgs.model_json_schema(),
)

#: Validation error text handed back to the model is capped as a whole.
_MAX_VALIDATION_CHARS = 2_000

_NO_TOOL_PROMPT = "Call `finish` when you are done, or use a tool."


class NativeRuntime:
    """Gaia's own tool loop over a provider-neutral :class:`ToolChatLLM`."""

    name = "native"

    def __init__(
        self,
        *,
        name: str = "native",
        llm_factory: Callable[[AgentTask], Awaitable[LlmHandle]],
        tools: ToolRegistry,
        context_factory: Callable[[AgentTask, EventSink], Awaitable[ToolContext]],
        max_tokens: int = 32_000,
        system_prompt: str = JUNIOR_SYSTEM_PROMPT,
    ) -> None:
        self.name = name
        self._llm_factory = llm_factory
        self._tools = tools
        self._context_factory = context_factory
        self._max_tokens = max_tokens
        self._system_prompt = system_prompt
        self._running: dict[str, asyncio.Task[AgentResult]] = {}
        self._handles: dict[str, LlmHandle] = {}

    async def run_task(self, task: AgentTask, events: EventSink) -> AgentResult:
        """Run one task, appending only to the message history.

        The loop runs in its **own child task** (lead review N1). ``cancel()``
        cancels only that child, and ``run_task`` then returns
        ``status="cancelled"``, the same contract the other runtimes keep.
        Cancelling the *caller* still works the usual way: asyncio cancels the
        awaited child too, its ``finally`` closes the LLM handle, and the
        ``CancelledError`` propagates. Registering ``asyncio.current_task()``
        instead would let ``cancel(task_id)`` tear down whatever called the
        runtime - the graph node.
        """
        inner = asyncio.create_task(self._run(task, events), name=f"native:{task.task_id}")
        self._running[task.task_id] = inner
        try:
            return await inner
        except asyncio.CancelledError:
            current = asyncio.current_task()
            if current is not None and current.cancelling():
                raise
            handle = self._handles.get(task.task_id)
            return _cancelled_result(handle.usage.total() if handle is not None else RuntimeUsage())
        finally:
            self._running.pop(task.task_id, None)
            self._handles.pop(task.task_id, None)

    async def cancel(self, task_id: str) -> None:
        """Cancel the task's loop, if it is running. Idempotent for unknown ids."""
        inner = self._running.get(task_id)
        if inner is not None:
            inner.cancel()

    async def _run(self, task: AgentTask, events: EventSink) -> AgentResult:
        handle = await self._llm_factory(task)
        self._handles[task.task_id] = handle
        try:
            ctx = await self._context_factory(task, events)
            return await self._run_loop(task, events, handle, ctx)
        finally:
            await _close_handle(handle)

    async def _run_loop(
        self,
        task: AgentTask,
        events: EventSink,
        handle: LlmHandle,
        ctx: ToolContext,
    ) -> AgentResult:
        messages: list[LLMMessage] = [
            LLMMessage(role="system", content=self._system_prompt),
            LLMMessage(role="user", content=task.instructions),
        ]
        tools = (*self._tools.specs(), FINISH_TOOL)
        consecutive_no_tool_turns = 0

        for turn in range(task.max_turns):
            try:
                response = await handle.llm.complete(
                    messages=messages,
                    tools=tools,
                    max_tokens=self._max_tokens,
                )
            except LLMBudgetExceeded:
                return _budget_result(handle.usage.total())

            if response.text:
                await events.emit(
                    EventType.AGENT_MESSAGE,
                    {"turn": turn, "content": response.text},
                )
            await _emit_turn_usage(events, turn, response.usage)

            # Append-only (doc 10 requirement 10): every earlier message stays a
            # frozen dataclass. DeepSeek's tool loop requires reasoning_content
            # on every assistant turn when tools are present, so carry it even
            # when this turn has no tool calls.
            messages.append(
                LLMMessage(
                    role="assistant",
                    content=response.text,
                    tool_calls=response.tool_calls,
                    reasoning_content=response.reasoning_content,
                )
            )

            if not response.tool_calls:
                messages.append(LLMMessage(role="user", content=_NO_TOOL_PROMPT))
                consecutive_no_tool_turns += 1
                if consecutive_no_tool_turns >= 3:
                    return _failed_result(
                        handle.usage.total(),
                        error="the model stopped without calling finish",
                    )
                continue

            consecutive_no_tool_turns = 0
            for call in response.tool_calls:
                if call.name == FINISH_TOOL_NAME:
                    try:
                        args = FinishArgs.model_validate(call.arguments)
                    except ValidationError as exc:
                        error = _format_validation_errors(exc)
                        messages.append(
                            LLMMessage(
                                role="tool",
                                tool_call_id=call.id,
                                content=f"invalid finish arguments:\n{error}",
                            )
                        )
                        # Keep going: every tool call in the assistant message
                        # must get a tool result, or an OpenAI-compatible API
                        # rejects the next request (lead review N2).
                        continue
                    return self._completed_result(args, response, handle)
                result = await self._tools.call(
                    call.name,
                    call.arguments,
                    ctx,
                    call_id=call.id,
                )
                messages.append(
                    LLMMessage(role="tool", tool_call_id=call.id, content=result.content)
                )

        return _failed_result(
            handle.usage.total(),
            error=f"max turns ({task.max_turns}) reached",
        )

    def _completed_result(
        self,
        args: FinishArgs,
        response: LLMResponse,
        handle: LlmHandle,
    ) -> AgentResult:
        """Return the completed result for valid ``finish`` arguments."""
        ignored = len(response.tool_calls) - 1
        summary = args.summary
        if ignored:
            summary = f"{summary}; ignored {ignored} tool call(s) after finish"
        return AgentResult(
            status="completed",
            summary=summary,
            structured_output=args.model_dump(),
            files_changed=(),
            usage=handle.usage.total(),
            error=None,
            transcript_ref=None,
        )


async def _close_handle(handle: LlmHandle | None) -> None:
    """Close ``handle`` even when the current task is being cancelled."""
    if handle is None:
        return
    close_task = asyncio.ensure_future(handle.close())
    await asyncio.shield(close_task)


async def _emit_turn_usage(events: EventSink, turn: int, usage: LLMUsage) -> None:
    """Emit the per-turn ``agent.usage`` event required by doc 10."""
    await events.emit(
        EventType.AGENT_USAGE,
        {
            "turn": turn,
            "provider": usage.provider,
            "model": usage.model,
            "input_tokens": usage.input_tokens,
            "output_tokens": usage.output_tokens,
            "cache_tokens": usage.cache_read_tokens + usage.cache_creation_tokens,
            "request_id": usage.request_id,
        },
    )


def _budget_result(usage: Any) -> AgentResult:
    return AgentResult(
        status="budget_exceeded",
        summary="LLM budget exceeded",
        structured_output=None,
        files_changed=(),
        usage=usage,
        error=None,
        transcript_ref=None,
    )


def _cancelled_result(usage: RuntimeUsage) -> AgentResult:
    return AgentResult(
        status="cancelled",
        summary="task cancelled",
        structured_output=None,
        files_changed=(),
        usage=usage,
        error=None,
        transcript_ref=None,
    )


def _failed_result(usage: Any, *, error: str) -> AgentResult:
    return AgentResult(
        status="failed",
        summary="task failed",
        structured_output=None,
        files_changed=(),
        usage=usage,
        error=error,
        transcript_ref=None,
    )


def _format_validation_errors(exc: ValidationError) -> str:
    lines = [
        f"{'.'.join(str(part) for part in error.get('loc', ()))}: {error.get('msg', 'invalid')}"
        for error in exc.errors()
    ]
    return "\n".join(lines)[:_MAX_VALIDATION_CHARS]
