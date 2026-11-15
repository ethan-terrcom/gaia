"""NativeRuntime loop tests (doc 10b)."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from gaia.domain.enums import AgentRole
from gaia.llm.base import (
    LLMBudgetExceeded,
    LLMMessage,
    LLMResponse,
    LLMTool,
    ToolCall,
)
from gaia.runtimes.approvals import InMemoryApprovalBroker
from gaia.runtimes.base import AgentResult, AgentTask
from gaia.runtimes.native import FINISH_TOOL, JUNIOR_SYSTEM_PROMPT, NativeRuntime, _close_handle
from gaia.runtimes.native_llm import LlmHandle, UsageTally
from gaia.runtimes.support import run_with_limits
from gaia.security.audit import InMemoryAuditSink
from gaia.security.commands import CommandPolicy
from gaia.services.events import EventSink
from gaia.services.git import GitService
from gaia.tools.registry import JUNIOR_TOOLS, ToolContext, ToolRegistry, build_default_registry
from tests.llm_fakes import ScriptedToolChatLLM, make_usage
from tests.runtime_contract import base_sha, make_worktree_location
from tests.tool_fakes import FakeCommandRunner, RecordingEventSink


class RaisingLLM:
    """A tool-chat LLM that always raises one exception."""

    def __init__(self, exc: Exception) -> None:
        self.exc = exc

    async def complete(
        self,
        *,
        messages: Sequence[LLMMessage],
        tools: Sequence[LLMTool] = (),
        max_tokens: int = 4096,
    ) -> LLMResponse:
        del messages, tools, max_tokens
        raise self.exc


@pytest.fixture
def worktree(git_repo: Path, tmp_path: Path):
    return make_worktree_location(git_repo, tmp_path)


def _task(
    worktree: Any,
    git_repo: Path,
    *,
    task_id: str = "task-native",
    max_turns: int = 40,
    timeout_s: float = 30.0,
) -> AgentTask:
    return AgentTask(
        task_id=task_id,
        run_id="run-native",
        run_step_id=None,
        workspace_id="ws-native",
        agent_id="agent-native",
        role=AgentRole.JUNIOR,
        instructions="do the step",
        worktree=worktree,
        base_sha=base_sha(git_repo),
        max_turns=max_turns,
        timeout_s=timeout_s,
    )


def _registry() -> ToolRegistry:
    return build_default_registry(search_supports_regex=False).subset(JUNIOR_TOOLS)


def _context_factory(
    git_service: GitService,
    tmp_path: Path,
    broker: InMemoryApprovalBroker,
) -> Callable[[AgentTask, EventSink], Awaitable[ToolContext]]:
    async def factory(task: AgentTask, events: EventSink) -> ToolContext:
        task_dir = tmp_path / f"task-{task.task_id}"
        task_dir.mkdir(parents=True, exist_ok=True)
        return ToolContext(
            task=task,
            git=git_service,
            policy=CommandPolicy.from_default(),
            approvals=broker,
            audit=InMemoryAuditSink(),
            events=events,
            commands=FakeCommandRunner(),
            task_dir=task_dir,
            actor="agent:native-test",
            rg_path=None,
        )

    return factory


def _finish_call(call_id: str = "call_finish", **kwargs: Any) -> ToolCall:
    arguments = {"summary": "done", "tests_passed": True, **kwargs}
    return ToolCall(id=call_id, name="finish", arguments=arguments)


def _response(
    text: str = "",
    tool_calls: tuple[ToolCall, ...] = (),
    *,
    reasoning_content: str | None = None,
) -> LLMResponse:
    return LLMResponse(
        text=text,
        usage=make_usage(),
        tool_calls=tool_calls,
        reasoning_content=reasoning_content,
    )


async def _run_direct(
    runtime: NativeRuntime,
    task: AgentTask,
    sink: EventSink,
) -> AgentResult:
    return await runtime.run_task(task, sink)


def test_system_prompt_is_the_file_bytes() -> None:
    prompt = JUNIOR_SYSTEM_PROMPT
    path = (
        Path(__file__).resolve().parents[1] / "gaia" / "runtimes" / "prompts" / "junior_system.md"
    )
    assert prompt == path.read_text(encoding="utf-8")


async def test_finish_tool_uses_finish_schema_and_is_not_a_registry_tool() -> None:
    registry = _registry()
    schema = FINISH_TOOL.input_schema
    assert FINISH_TOOL.name == "finish"
    assert schema["type"] == "object"
    assert schema["additionalProperties"] is False
    assert schema["required"] == ["summary", "tests_passed"]
    assert schema["properties"]["tests_passed"]["type"] == "boolean"
    assert schema["properties"]["summary"]["type"] == "string"
    assert "finish" not in registry.names()


async def test_reasoning_content_is_passed_back_on_every_later_request(
    worktree: Any,
    git_repo: Path,
    git_service: GitService,
    tmp_path: Path,
) -> None:
    llm = ScriptedToolChatLLM(
        [
            _response("first", reasoning_content="think1"),
            _response("second", reasoning_content="think2"),
            _response(tool_calls=(_finish_call(),)),
        ]
    )
    handle = LlmHandle(llm=llm, usage=UsageTally(), close=_noop_close)
    broker = InMemoryApprovalBroker()
    runtime = NativeRuntime(
        tools=_registry(),
        llm_factory=_handle_factory(handle),
        context_factory=_context_factory(git_service, tmp_path, broker),
    )

    result = await _run_direct(runtime, _task(worktree, git_repo), RecordingEventSink())

    assert result.status == "completed"
    assert len(llm.calls) == 3
    first_assistant = _assistant_messages(llm.calls[1].messages)
    assert first_assistant[0].reasoning_content == "think1"
    second_assistant = _assistant_messages(llm.calls[2].messages)
    assert second_assistant[0].reasoning_content == "think1"
    assert second_assistant[1].reasoning_content == "think2"


async def test_history_is_append_only_across_turns(
    worktree: Any,
    git_repo: Path,
    git_service: GitService,
    tmp_path: Path,
) -> None:
    llm = ScriptedToolChatLLM(
        [
            _response("first"),
            _response(tool_calls=(_finish_call(),)),
        ]
    )
    handle = LlmHandle(llm=llm, usage=UsageTally(), close=_noop_close)
    runtime = NativeRuntime(
        tools=_registry(),
        llm_factory=_handle_factory(handle),
        context_factory=_context_factory(git_service, tmp_path, InMemoryApprovalBroker()),
    )

    await _run_direct(runtime, _task(worktree, git_repo), RecordingEventSink())

    assert len(llm.calls) == 2
    first_messages = llm.calls[0].messages
    second_messages = llm.calls[1].messages
    for index, message in enumerate(first_messages):
        assert second_messages[index] is message


async def test_three_no_tool_turns_fail(
    worktree: Any,
    git_repo: Path,
    git_service: GitService,
    tmp_path: Path,
) -> None:
    llm = ScriptedToolChatLLM([_response("one"), _response("two"), _response("three")])
    handle = LlmHandle(llm=llm, usage=UsageTally(), close=_noop_close)
    runtime = NativeRuntime(
        tools=_registry(),
        llm_factory=_handle_factory(handle),
        context_factory=_context_factory(git_service, tmp_path, InMemoryApprovalBroker()),
    )

    result = await _run_direct(runtime, _task(worktree, git_repo), RecordingEventSink())

    assert result.status == "failed"
    assert result.error == "the model stopped without calling finish"
    assert len(llm.calls) == 3


async def test_invalid_finish_arguments_are_fed_back_and_loop_continues(
    worktree: Any,
    git_repo: Path,
    git_service: GitService,
    tmp_path: Path,
) -> None:
    bad_finish = ToolCall(
        id="call_bad",
        name="finish",
        arguments={"summary": "missing tests_passed"},
    )
    llm = ScriptedToolChatLLM(
        [
            _response(tool_calls=(bad_finish,)),
            _response(tool_calls=(_finish_call(),)),
        ]
    )
    handle = LlmHandle(llm=llm, usage=UsageTally(), close=_noop_close)
    runtime = NativeRuntime(
        tools=_registry(),
        llm_factory=_handle_factory(handle),
        context_factory=_context_factory(git_service, tmp_path, InMemoryApprovalBroker()),
    )

    result = await _run_direct(runtime, _task(worktree, git_repo), RecordingEventSink())

    assert result.status == "completed"
    assert len(llm.calls) == 2
    tool_messages = _tool_messages(llm.calls[1].messages)
    assert any("invalid finish arguments" in message.content for message in tool_messages)


async def test_max_turns_reached(
    worktree: Any,
    git_repo: Path,
    git_service: GitService,
    tmp_path: Path,
) -> None:
    llm = ScriptedToolChatLLM([_response("still not done")])
    handle = LlmHandle(llm=llm, usage=UsageTally(), close=_noop_close)
    runtime = NativeRuntime(
        tools=_registry(),
        llm_factory=_handle_factory(handle),
        context_factory=_context_factory(git_service, tmp_path, InMemoryApprovalBroker()),
    )

    result = await _run_direct(
        runtime,
        _task(worktree, git_repo, max_turns=1),
        RecordingEventSink(),
    )

    assert result.status == "failed"
    assert result.error == "max turns (1) reached"


async def test_unknown_tool_goes_through_registry_and_error_is_returned_to_model(
    worktree: Any,
    git_repo: Path,
    git_service: GitService,
    tmp_path: Path,
) -> None:
    unknown = ToolCall(id="call_unknown", name="does_not_exist", arguments={})
    llm = ScriptedToolChatLLM(
        [
            _response(tool_calls=(unknown,)),
            _response(tool_calls=(_finish_call(),)),
        ]
    )
    handle = LlmHandle(llm=llm, usage=UsageTally(), close=_noop_close)
    runtime = NativeRuntime(
        tools=_registry(),
        llm_factory=_handle_factory(handle),
        context_factory=_context_factory(git_service, tmp_path, InMemoryApprovalBroker()),
    )

    result = await _run_direct(runtime, _task(worktree, git_repo), RecordingEventSink())

    assert result.status == "completed"
    tool_messages = _tool_messages(llm.calls[1].messages)
    assert any("unknown tool: does_not_exist" in message.content for message in tool_messages)


async def test_llm_budget_exceeded_returns_budget_exceeded(
    worktree: Any,
    git_repo: Path,
    git_service: GitService,
    tmp_path: Path,
) -> None:
    handle = LlmHandle(
        llm=RaisingLLM(LLMBudgetExceeded()),
        usage=UsageTally(),
        close=_noop_close,
    )
    runtime = NativeRuntime(
        tools=_registry(),
        llm_factory=_handle_factory(handle),
        context_factory=_context_factory(git_service, tmp_path, InMemoryApprovalBroker()),
    )

    result = await _run_direct(runtime, _task(worktree, git_repo), RecordingEventSink())

    assert result.status == "budget_exceeded"
    assert result.error is None


async def test_handle_closed_on_success(
    worktree: Any,
    git_repo: Path,
    git_service: GitService,
    tmp_path: Path,
) -> None:
    closed = asyncio.Event()
    llm = ScriptedToolChatLLM([_response(tool_calls=(_finish_call(),))])
    handle = LlmHandle(llm=llm, usage=UsageTally(), close=_make_close(closed))
    runtime = NativeRuntime(
        tools=_registry(),
        llm_factory=_handle_factory(handle),
        context_factory=_context_factory(git_service, tmp_path, InMemoryApprovalBroker()),
    )

    await _run_direct(runtime, _task(worktree, git_repo), RecordingEventSink())

    assert closed.is_set()


async def test_handle_closed_on_cancel(
    worktree: Any,
    git_repo: Path,
    git_service: GitService,
    tmp_path: Path,
) -> None:
    closed = asyncio.Event()
    block = asyncio.Event()
    llm = ScriptedToolChatLLM([], block=block)
    handle = LlmHandle(llm=llm, usage=UsageTally(), close=_make_close(closed))
    broker = InMemoryApprovalBroker()
    runtime = NativeRuntime(
        tools=_registry(),
        llm_factory=_handle_factory(handle),
        context_factory=_context_factory(git_service, tmp_path, broker),
    )
    task = _task(worktree, git_repo, task_id="task-cancel")

    run_task = asyncio.create_task(runtime.run_task(task, RecordingEventSink()))
    await llm.started.wait()
    await runtime.cancel(task.task_id)
    result = await run_task

    assert result.status == "cancelled"
    assert closed.is_set()
    assert llm.cancelled


async def test_handle_closed_on_failure(
    worktree: Any,
    git_repo: Path,
    git_service: GitService,
    tmp_path: Path,
) -> None:
    closed = asyncio.Event()
    handle = LlmHandle(
        llm=RaisingLLM(RuntimeError("boom")),
        usage=UsageTally(),
        close=_make_close(closed),
    )
    runtime = NativeRuntime(
        tools=_registry(),
        llm_factory=_handle_factory(handle),
        context_factory=_context_factory(git_service, tmp_path, InMemoryApprovalBroker()),
    )
    task = _task(worktree, git_repo)
    sink = RecordingEventSink()

    result = await run_with_limits(runtime, task, sink, git=git_service)

    assert result.status == "failed"
    assert closed.is_set()


async def test_tool_calls_after_finish_are_ignored_and_noted(
    worktree: Any,
    git_repo: Path,
    git_service: GitService,
    tmp_path: Path,
) -> None:
    write_after = ToolCall(
        id="call_after",
        name="write_file",
        arguments={"path": "ignored.txt", "content": "x"},
    )
    llm = ScriptedToolChatLLM([_response(tool_calls=(_finish_call(), write_after))])
    handle = LlmHandle(llm=llm, usage=UsageTally(), close=_noop_close)
    runtime = NativeRuntime(
        tools=_registry(),
        llm_factory=_handle_factory(handle),
        context_factory=_context_factory(git_service, tmp_path, InMemoryApprovalBroker()),
    )

    result = await _run_direct(runtime, _task(worktree, git_repo), RecordingEventSink())

    assert result.status == "completed"
    assert "ignored 1 tool call(s) after finish" in result.summary
    assert not (worktree.work_tree / "ignored.txt").exists()


async def test_context_factory_failure_closes_handle_and_raises(
    worktree: Any,
    git_repo: Path,
    git_service: GitService,
    tmp_path: Path,
) -> None:
    closed = asyncio.Event()
    handle = LlmHandle(
        llm=ScriptedToolChatLLM([]),
        usage=UsageTally(),
        close=_make_close(closed),
    )

    async def bad_context_factory(task: AgentTask, events: EventSink) -> ToolContext:
        del task, events
        raise RuntimeError("bad context")

    runtime = NativeRuntime(
        tools=_registry(),
        llm_factory=_handle_factory(handle),
        context_factory=bad_context_factory,
    )

    with pytest.raises(RuntimeError, match="bad context"):
        await runtime.run_task(_task(worktree, git_repo), RecordingEventSink())

    assert closed.is_set()


async def test_close_handle_none_is_noop() -> None:
    await _close_handle(None)


async def test_run_task_registers_only_when_current_task_exists(
    worktree: Any,
    git_repo: Path,
    git_service: GitService,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    llm = ScriptedToolChatLLM([_response(tool_calls=(_finish_call(),))])
    handle = LlmHandle(llm=llm, usage=UsageTally(), close=_noop_close)
    runtime = NativeRuntime(
        tools=_registry(),
        llm_factory=_handle_factory(handle),
        context_factory=_context_factory(git_service, tmp_path, InMemoryApprovalBroker()),
    )
    monkeypatch.setattr(asyncio, "current_task", lambda: None)

    result = await _run_direct(runtime, _task(worktree, git_repo), RecordingEventSink())

    assert result.status == "completed"
    assert runtime._running == {}


async def test_usage_tally_sums_tokens_and_cost() -> None:
    tally = UsageTally()
    await tally.record(
        make_usage(input_tokens=5, output_tokens=3, cache_read_tokens=2),
        cost_usd=Decimal("0.5"),
    )
    await tally.record(
        make_usage(input_tokens=7, output_tokens=4, cache_read_tokens=1, cache_creation_tokens=2),
        cost_usd=Decimal("0.25"),
    )

    total = tally.total()

    assert total.input_tokens == 12
    assert total.output_tokens == 7
    assert total.cache_tokens == 5
    assert total.cost_usd == Decimal("0.75")


async def _noop_close() -> None:
    return None


def _make_close(event: asyncio.Event) -> Callable[[], Awaitable[None]]:
    async def close() -> None:
        event.set()

    return close


def _handle_factory(handle: LlmHandle) -> Callable[[AgentTask], Awaitable[LlmHandle]]:
    async def factory(task: AgentTask) -> LlmHandle:
        del task
        return handle

    return factory


def _assistant_messages(messages: Sequence[LLMMessage]) -> list[LLMMessage]:
    return [message for message in messages if message.role == "assistant"]


def _tool_messages(messages: Sequence[LLMMessage]) -> list[LLMMessage]:
    return [message for message in messages if message.role == "tool"]


async def test_every_tool_call_is_answered_after_an_invalid_finish(
    worktree: Any,
    git_repo: Path,
    git_service: GitService,
    tmp_path: Path,
) -> None:
    """Lead review N2: OpenAI-compatible APIs reject a request in which any earlier
    tool call has no tool result, so a bad ``finish`` must not skip the rest."""
    bad_finish = ToolCall(id="call_bad", name="finish", arguments={"summary": "no tests flag"})
    status = ToolCall(id="call_status", name="git_status", arguments={})
    llm = ScriptedToolChatLLM(
        [
            _response(tool_calls=(bad_finish, status)),
            _response(tool_calls=(_finish_call(),)),
        ]
    )
    handle = LlmHandle(llm=llm, usage=UsageTally(), close=_noop_close)
    runtime = NativeRuntime(
        tools=_registry(),
        llm_factory=_handle_factory(handle),
        context_factory=_context_factory(git_service, tmp_path, InMemoryApprovalBroker()),
    )

    result = await _run_direct(runtime, _task(worktree, git_repo), RecordingEventSink())

    assert result.status == "completed"
    answered = {message.tool_call_id for message in _tool_messages(llm.calls[1].messages)}
    assert {"call_bad", "call_status"} <= answered


async def test_cancelling_the_caller_still_propagates_and_closes_the_handle(
    worktree: Any,
    git_repo: Path,
    git_service: GitService,
    tmp_path: Path,
) -> None:
    """Lead review N1: ``cancel(task_id)`` returns ``cancelled``, but cancelling the
    caller (the graph node) must still raise there, after the loop cleaned up."""
    closed: list[bool] = []

    async def _close() -> None:
        closed.append(True)

    llm = ScriptedToolChatLLM([], block=asyncio.Event())
    handle = LlmHandle(llm=llm, usage=UsageTally(), close=_close)
    runtime = NativeRuntime(
        tools=_registry(),
        llm_factory=_handle_factory(handle),
        context_factory=_context_factory(git_service, tmp_path, InMemoryApprovalBroker()),
    )
    caller = asyncio.create_task(
        _run_direct(runtime, _task(worktree, git_repo), RecordingEventSink())
    )
    await llm.started.wait()
    caller.cancel()

    with pytest.raises(asyncio.CancelledError):
        await caller
    assert llm.cancelled
    assert closed == [True]
    assert runtime._running == {}


async def test_cancel_returns_cancelled_and_leaves_the_caller_running(
    worktree: Any,
    git_repo: Path,
    git_service: GitService,
    tmp_path: Path,
) -> None:
    llm = ScriptedToolChatLLM([], block=asyncio.Event())
    handle = LlmHandle(llm=llm, usage=UsageTally(), close=_noop_close)
    runtime = NativeRuntime(
        tools=_registry(),
        llm_factory=_handle_factory(handle),
        context_factory=_context_factory(git_service, tmp_path, InMemoryApprovalBroker()),
    )
    task = _task(worktree, git_repo)

    async def caller() -> tuple[str, str]:
        result = await runtime.run_task(task, RecordingEventSink())
        # The caller is still alive and can keep going after the agent stopped.
        await asyncio.sleep(0)
        return result.status, "caller continued"

    running = asyncio.create_task(caller())
    await llm.started.wait()
    await runtime.cancel(task.task_id)

    assert await asyncio.wait_for(running, timeout=5) == ("cancelled", "caller continued")
