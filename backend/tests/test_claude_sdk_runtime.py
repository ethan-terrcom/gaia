"""Unit tests for :class:`gaia.runtimes.claude_agent_sdk.ClaudeAgentSdkRuntime`."""

from __future__ import annotations

import asyncio
import os
from collections.abc import Sequence
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from claude_agent_sdk import HookMatcher, ResultMessage
from pydantic import BaseModel, SecretStr

import gaia.runtimes.claude_agent_sdk as claude_runtime
from gaia.config.settings import Settings
from gaia.db.ids import uuid7
from gaia.domain.enums import EventType
from gaia.runtimes.approvals import InMemoryApprovalBroker
from gaia.runtimes.base import AgentTask
from gaia.runtimes.claude_agent_sdk import (
    _DISALLOWED_TOOLS,
    ClaudeAgentSdkRuntime,
    _best_effort,
    _bundled_cli,
    _is_max_turns,
    _short_result_error,
    _usage_int,
    build_wrapper_script,
)
from gaia.runtimes.orphans import OrphanRegistry
from gaia.security.audit import InMemoryAuditSink
from gaia.security.commands import CommandPolicy
from gaia.services.costs import ModelPrice, PriceRates, PriceTable
from gaia.services.git import GitService
from gaia.services.proc import ProcResult
from gaia.tools.registry import Tool, ToolContext, ToolInput, ToolRegistry, ToolResult
from tests.runtime_contract import RecordingSink, base_sha, make_task, make_worktree_location
from tests.sdk_fakes import (
    FakeSdkClient,
    SdkBlock,
    SdkToolCall,
    assistant_text,
    error_result,
    success_result,
)

FAKE_RUN_TOKEN = "gaia_rt_test-token"
FAKE_BASE_URL = "http://127.0.0.1:8765/llm/anthropic"


class _EchoInput(ToolInput):
    text: str


async def _echo_handler(input: BaseModel, ctx: ToolContext) -> ToolResult:
    del ctx
    model = _EchoInput.model_validate(input)
    return ToolResult(True, f"echo:{model.text}")


def _test_registry() -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(
        Tool(
            name="echo",
            description="Echo text back.",
            input_model=_EchoInput,
            handler=_echo_handler,
            mutates=False,
        )
    )
    return registry


class _UnusedCommandRunner:
    async def run(
        self,
        argv: Sequence[str],
        *,
        cwd: Path,
        timeout_s: float,
        task_dir: Path,
    ) -> ProcResult:
        del argv, cwd, timeout_s, task_dir
        raise AssertionError("run_command must not be called")


class _FakeMinted:
    def __init__(self) -> None:
        self.token_id = uuid7()
        self.token = SecretStr(FAKE_RUN_TOKEN)


async def _fake_mint(task: AgentTask) -> Any:
    del task
    return _FakeMinted()


async def _fake_revoke(task: AgentTask, token_id: str) -> None:
    del task, token_id


async def _budget_false(task: AgentTask) -> bool:
    del task
    return False


async def _budget_true(task: AgentTask) -> bool:
    del task
    return True


def _make_settings(tmp_path: Path) -> Settings:
    srt_path = tmp_path / "srt"
    settings = Settings(data_dir=tmp_path / "gaia-data", srt_path=srt_path)
    srt_path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    srt_path.chmod(0o700)
    return settings


def _price_table() -> PriceTable:
    return PriceTable(
        {
            "test-model": ModelPrice(
                rates=PriceRates(
                    input=Decimal(1),
                    output=Decimal(2),
                    cache_read=Decimal("0.5"),
                    cache_write=Decimal("0.25"),
                )
            )
        }
    )


class _PidWritingClient(FakeSdkClient):
    """A fake that writes the wrapper pid file after connect, like the wrapper."""

    def __init__(self, options: Any, script: list[Any], *, runtime: Any) -> None:
        super().__init__(options, script, runtime=runtime)
        self.pid_file = runtime.active_pid_file if runtime is not None else None

    async def __aenter__(self) -> FakeSdkClient:
        if self.pid_file is not None:
            self.pid_file.write_text(str(os.getpid()), encoding="ascii")
        return await super().__aenter__()


def _make_runtime(
    tmp_path: Path,
    git_service: GitService,
    *,
    script: list[Any],
    registry: ToolRegistry | None = None,
    budget_exhausted: Any = None,
    model: str = "test-model",
    price_table: PriceTable | None = None,
    orphans: OrphanRegistry | None = None,
    client_factory: Any = None,
    settings: Settings | None = None,
) -> ClaudeAgentSdkRuntime:
    settings = settings or _make_settings(tmp_path)

    async def context_factory(task: AgentTask, events: Any, task_dir: Path) -> ToolContext:
        return ToolContext(
            task=task,
            git=git_service,
            policy=CommandPolicy.from_default(),
            approvals=InMemoryApprovalBroker(),
            audit=InMemoryAuditSink(),
            events=events,
            commands=_UnusedCommandRunner(),
            task_dir=task_dir,
            actor="test-agent",
            approval_timeout_s=600,
            rg_path=None,
        )

    runtime: ClaudeAgentSdkRuntime | None = None

    def default_client_factory(options: Any) -> FakeSdkClient:
        assert runtime is not None
        return FakeSdkClient(options, script, runtime=runtime)

    runtime = ClaudeAgentSdkRuntime(
        registry=registry or _test_registry(),
        settings=settings,
        sessions=None,
        context_factory=context_factory,
        model=model,
        client_factory=client_factory or default_client_factory,
        price_table=price_table or _price_table(),
        orphans=orphans,
        budget_exhausted=budget_exhausted or _budget_false,
        mint_token=_fake_mint,
        revoke_token=_fake_revoke,
    )
    return runtime


@pytest.fixture
def worktree(git_repo: Path, tmp_path: Path) -> Any:
    return make_worktree_location(git_repo, tmp_path)


def _task(worktree: Any, git_repo: Path, **kwargs: Any) -> AgentTask:
    return make_task(worktree, base_sha=base_sha(git_repo), **kwargs)


async def test_options_disable_builtins_and_scrub_env(
    worktree: Any,
    git_repo: Path,
    git_service: GitService,
    tmp_path: Path,
) -> None:
    captured: list[Any] = []

    def capturing_factory(options: Any) -> FakeSdkClient:
        captured.append(options)
        return FakeSdkClient(options, [success_result()])

    runtime = _make_runtime(
        tmp_path,
        git_service,
        script=[success_result()],
        client_factory=capturing_factory,
    )
    task = _task(worktree, git_repo)

    result = await runtime.run_task(task, RecordingSink())

    assert result.status == "failed"
    options = captured[0]
    assert options.tools == []
    assert options.setting_sources == []
    assert options.strict_mcp_config is True
    assert options.permission_mode == "dontAsk"
    assert options.allowed_tools == ["mcp__gaia__echo", "mcp__gaia__finish"]
    assert options.disallowed_tools == list(_DISALLOWED_TOOLS)
    assert options.env == {
        "GAIA_RUN_TOKEN": FAKE_RUN_TOKEN,
        "GAIA_ANTHROPIC_BASE_URL": FAKE_BASE_URL,
    }
    assert "ANTHROPIC_API_KEY" not in options.env
    assert options.cli_path is not None
    assert Path(options.cli_path).name == "claude-wrapper"
    assert options.resume is None
    assert options.continue_conversation is False
    assert options.fork_session is False
    assert options.enable_file_checkpointing is False
    assert options.hooks is not None
    assert "PreToolUse" in options.hooks
    assert isinstance(options.hooks["PreToolUse"][0], HookMatcher)
    assert options.mcp_servers is not None
    assert "gaia" in options.mcp_servers


async def test_tool_bridge_calls_the_real_registry(
    worktree: Any,
    git_repo: Path,
    git_service: GitService,
    tmp_path: Path,
) -> None:
    script: list[Any] = [
        SdkToolCall("echo", {"text": "hello"}),
        SdkToolCall(
            "finish",
            {
                "summary": "done",
                "tests_run": ["pytest -q"],
                "tests_passed": True,
                "concerns": [],
            },
        ),
        success_result(),
    ]
    runtime = _make_runtime(tmp_path, git_service, script=script)
    sink = RecordingSink()
    task = _task(worktree, git_repo)

    result = await runtime.run_task(task, sink)

    assert result.status == "completed"
    assert result.summary == "done"
    assert result.structured_output == {
        "summary": "done",
        "tests_run": ["pytest -q"],
        "tests_passed": True,
        "concerns": [],
    }
    assert any(
        event_type == EventType.AGENT_TOOL_CALL and payload.get("tool") == "echo"
        for event_type, payload in sink.events
    )
    assert any(
        event_type == EventType.AGENT_TOOL_RESULT
        and payload.get("tool") == "echo"
        and payload.get("content") == "echo:hello"
        for event_type, payload in sink.events
    )


async def test_finish_tool_validation_and_storage(
    worktree: Any,
    git_repo: Path,
    git_service: GitService,
    tmp_path: Path,
) -> None:
    runtime = _make_runtime(tmp_path, git_service, script=[])
    task = _task(worktree, git_repo)

    invalid = await runtime._finish_tool(
        task,
        {"summary": "", "tests_run": [], "tests_passed": True, "concerns": []},
    )
    assert invalid["is_error"] is True
    assert "summary" in invalid["content"][0]["text"]

    valid = await runtime._finish_tool(
        task,
        {"summary": "done", "tests_run": [], "tests_passed": True, "concerns": []},
    )
    assert valid["is_error"] is False
    assert runtime._finish_args[task.task_id].summary == "done"


async def test_max_turns_and_budget_and_missing_finish_mapping(
    worktree: Any,
    git_repo: Path,
    git_service: GitService,
    tmp_path: Path,
) -> None:
    max_turns = ResultMessage(
        subtype="error_max_turns",
        duration_ms=1,
        duration_api_ms=1,
        is_error=True,
        num_turns=3,
        session_id="test-session",
        errors=["max turns"],
    )
    runtime = _make_runtime(tmp_path, git_service, script=[max_turns])
    result = await runtime.run_task(_task(worktree, git_repo), RecordingSink())
    assert result.status == "failed"
    assert result.error == "max turns reached"

    budget_runtime = _make_runtime(
        tmp_path,
        git_service,
        script=[error_result("api error")],
        budget_exhausted=_budget_true,
    )
    result = await budget_runtime.run_task(_task(worktree, git_repo), RecordingSink())
    assert result.status == "budget_exceeded"

    missing_finish_runtime = _make_runtime(
        tmp_path,
        git_service,
        script=[assistant_text("done"), success_result()],
    )
    result = await missing_finish_runtime.run_task(_task(worktree, git_repo), RecordingSink())
    assert result.status == "failed"
    assert result.error == "Claude Agent SDK result subtype success"


async def test_usage_is_priced_by_gaia_not_the_sdk(
    worktree: Any,
    git_repo: Path,
    git_service: GitService,
    tmp_path: Path,
) -> None:
    script: list[Any] = [
        SdkToolCall(
            "finish",
            {
                "summary": "done",
                "tests_run": [],
                "tests_passed": True,
                "concerns": [],
            },
        ),
        success_result(
            usage={
                "input_tokens": 100,
                "output_tokens": 20,
                "cache_read_input_tokens": 10,
                "cache_creation_input_tokens": 5,
            }
        ),
    ]
    runtime = _make_runtime(tmp_path, git_service, script=script)

    result = await runtime.run_task(_task(worktree, git_repo), RecordingSink())

    assert result.status == "completed"
    assert result.usage.input_tokens == 100
    assert result.usage.output_tokens == 20
    assert result.usage.cache_tokens == 15
    # (100*1 + 20*2 + 10*0.5 + 5*0.25) / 1_000_000
    assert result.usage.cost_usd == Decimal("0.00014625")


async def test_pre_tool_use_hook_denies_everything_but_gaia_tools(
    worktree: Any,
    git_repo: Path,
    git_service: GitService,
    tmp_path: Path,
) -> None:
    runtime = _make_runtime(tmp_path, git_service, script=[])

    denied = await runtime._pre_tool_use({"tool_name": "Bash"}, None, {})
    assert denied["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert (
        denied["hookSpecificOutput"]["permissionDecisionReason"] == "only Gaia tools are available"
    )

    allowed = await runtime._pre_tool_use({"tool_name": "mcp__gaia__echo"}, None, {})
    assert allowed == {}


async def test_cancel_interrupts_then_cancels(
    worktree: Any,
    git_repo: Path,
    git_service: GitService,
    tmp_path: Path,
) -> None:
    runtime = _make_runtime(tmp_path, git_service, script=[SdkBlock()])
    task = _task(worktree, git_repo)
    sink = RecordingSink()

    run_task = asyncio.create_task(runtime.run_task(task, sink))
    for _ in range(1000):
        if task.task_id in runtime._clients:
            break
        await asyncio.sleep(0.001)

    await runtime.cancel(task.task_id)
    result = await asyncio.wait_for(run_task, timeout=5)

    assert result.status == "cancelled"
    assert task.task_id not in runtime._clients
    assert task.task_id not in runtime._run_tasks


async def test_orphan_is_added_after_connect_and_removed_in_finally(
    worktree: Any,
    git_repo: Path,
    git_service: GitService,
    tmp_path: Path,
) -> None:
    registry = OrphanRegistry(tmp_path / "runtime-pids.json")
    runtime: ClaudeAgentSdkRuntime | None = None

    def pid_writing_factory(options: Any) -> _PidWritingClient:
        assert runtime is not None
        return _PidWritingClient(options, [SdkBlock()], runtime=runtime)

    runtime = _make_runtime(
        tmp_path,
        git_service,
        script=[SdkBlock()],
        orphans=registry,
        client_factory=pid_writing_factory,
    )
    task = _task(worktree, git_repo)
    run_task = asyncio.create_task(runtime.run_task(task, RecordingSink()))
    for _ in range(1000):
        if task.task_id in runtime._clients and runtime._sdk_tools:
            break
        await asyncio.sleep(0.001)

    await runtime.cancel(task.task_id)
    await asyncio.wait_for(run_task, timeout=5)

    assert registry._read() == []


def test_bundled_cli_exists() -> None:
    assert _bundled_cli().is_file()


def test_module_source_never_uses_banned_sdk_state_or_cost(tmp_path: Path) -> None:
    module = Path(__file__).resolve().parents[1] / "gaia" / "runtimes" / "claude_agent_sdk.py"
    banned = (
        "resume",
        "continue_conversation",
        "fork_session",
        "enable_file_checkpointing",
        "total_cost_usd",
        "model_usage",
    )
    for line_number, line in enumerate(module.read_text(encoding="utf-8").splitlines(), 1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        for name in banned:
            assert name not in line, (
                f"{module.name}:{line_number}: banned name {name!r} appears outside a comment"
            )


def test_build_wrapper_script_is_importable_for_public_surface() -> None:
    assert callable(build_wrapper_script)


async def test_sdk_tool_lookup_raises_for_unknown_task_or_tool(
    worktree: Any,
    git_repo: Path,
    git_service: GitService,
    tmp_path: Path,
) -> None:
    runtime = _make_runtime(tmp_path, git_service, script=[])
    task = _task(worktree, git_repo)
    with pytest.raises(LookupError, match="no SDK tool"):
        runtime.sdk_tool(task.task_id, "missing")
    with pytest.raises(LookupError, match="no SDK tool"):
        runtime.sdk_tool("missing-task", "echo")


async def test_default_token_and_budget_helpers_fail_closed_or_require_sessions(
    worktree: Any,
    git_repo: Path,
    git_service: GitService,
    tmp_path: Path,
) -> None:
    runtime = _make_runtime(tmp_path, git_service, script=[])
    task = _task(worktree, git_repo)

    with pytest.raises(ValueError, match="sessions are required"):
        await runtime._mint_token_default(task)
    with pytest.raises(ValueError, match="sessions are required"):
        await runtime._revoke_token_default(task, "token-id")
    assert await runtime._budget_exhausted_default(task) is False


async def test_pid_file_reader_times_out_cleanly(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(claude_runtime, "_PID_FILE_TIMEOUT_S", 0.0)
    assert await claude_runtime._read_pid_file(tmp_path / "missing-pid") is None


async def test_usage_and_error_helpers_handle_bad_input() -> None:
    assert _usage_int({}, "input_tokens") == 0
    assert _usage_int({"input_tokens": "not-an-int"}, "input_tokens") == 0
    assert _usage_int({"input_tokens": -5}, "input_tokens") == 0

    assert _short_result_error(None) == "Claude Agent SDK ended without a result"
    message = ResultMessage(
        subtype="error_during_execution",
        duration_ms=1,
        duration_api_ms=1,
        is_error=True,
        num_turns=1,
        session_id="test-session",
        errors=["first error", "second error"],
    )
    assert _short_result_error(message) == "first error; second error"

    terminal = ResultMessage(
        subtype="success",
        duration_ms=1,
        duration_api_ms=1,
        is_error=False,
        num_turns=1,
        session_id="test-session",
        terminal_reason="max_turns",
    )
    assert _is_max_turns(terminal) is True


async def test_best_effort_swallows_cleanup_errors() -> None:
    async def boom() -> None:
        raise RuntimeError("boom")

    await _best_effort(boom())
