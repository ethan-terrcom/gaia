"""Unit tests for :class:`gaia.runtimes.claude_agent_sdk.ClaudeAgentSdkRuntime`."""

from __future__ import annotations

import asyncio
import os
from collections.abc import Sequence
from decimal import Decimal
from pathlib import Path
from typing import Any

import claude_agent_sdk
import pytest
from claude_agent_sdk import (
    AssistantMessage,
    HookMatcher,
    ResultMessage,
    SystemMessage,
    ThinkingBlock,
)
from pydantic import BaseModel, SecretStr

import gaia.runtimes.claude_agent_sdk as claude_runtime
from gaia.config.settings import Settings
from gaia.db.engine import session_scope
from gaia.db.ids import uuid7
from gaia.db.models import Repository, Run, Ticket, User, Workspace
from gaia.db.repo import ScopedRepo
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
from tests.conftest import Database
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


class _CancelAfterQueryClient(_PidWritingClient):
    """A fake that asks ``_run`` to return ``cancelled`` after the query."""

    async def query(self, prompt: str, session_id: str = "default") -> None:
        await super().query(prompt, session_id)
        if self.runtime is not None and self.task_id:
            self.runtime._cancel_requested.add(self.task_id)


class _InterruptClient(FakeSdkClient):
    """A fake that raises ``exc`` from :meth:`interrupt` for cancel tests."""

    def __init__(self, exc: Exception) -> None:
        super().__init__(None, [])
        self._exc = exc

    async def interrupt(self) -> None:
        raise self._exc


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
    sessions: Any = None,
    mint_token: Any = _fake_mint,
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
        sessions=sessions,
        context_factory=context_factory,
        model=model,
        client_factory=client_factory or default_client_factory,
        price_table=price_table or _price_table(),
        orphans=orphans,
        budget_exhausted=budget_exhausted or _budget_false,
        mint_token=mint_token,
        revoke_token=_fake_revoke,
    )
    return runtime


def _make_runtime_with_pid_file(
    tmp_path: Path,
    git_service: GitService,
    *,
    script: list[Any],
    **kwargs: Any,
) -> ClaudeAgentSdkRuntime:
    """Build a runtime whose fake client writes the wrapper pid file."""
    holder: list[ClaudeAgentSdkRuntime] = []

    def client_factory(options: Any) -> _PidWritingClient:
        return _PidWritingClient(options, script, runtime=holder[0])

    runtime = _make_runtime(
        tmp_path,
        git_service,
        script=script,
        client_factory=client_factory,
        **kwargs,
    )
    holder.append(runtime)
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
    # Wait for the entry itself, not just the client: cancelling before the pid
    # file is read made this test pass vacuously and its coverage flaky (lead).
    for _ in range(5000):
        if registry._read():
            break
        await asyncio.sleep(0.001)
    assert [entry.task_id for entry in registry._read()] == [task.task_id]

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


async def test_cancel_with_no_live_client(
    worktree: Any,
    git_repo: Path,
    git_service: GitService,
    tmp_path: Path,
) -> None:
    runtime = _make_runtime(tmp_path, git_service, script=[])
    await runtime.cancel("missing-task")
    assert "missing-task" in runtime._cancel_requested


async def test_cancel_interrupt_timeout_branch(
    worktree: Any,
    git_repo: Path,
    git_service: GitService,
    tmp_path: Path,
) -> None:
    runtime = _make_runtime(tmp_path, git_service, script=[])
    runtime._clients["timeout-task"] = _InterruptClient(TimeoutError())
    await runtime.cancel("timeout-task")
    assert "timeout-task" in runtime._cancel_requested


async def test_cancel_interrupt_failure_branch(
    worktree: Any,
    git_repo: Path,
    git_service: GitService,
    tmp_path: Path,
) -> None:
    runtime = _make_runtime(tmp_path, git_service, script=[])
    runtime._clients["raise-task"] = _InterruptClient(RuntimeError("boom"))
    await runtime.cancel("raise-task")
    assert "raise-task" in runtime._cancel_requested


async def test_cancel_requested_before_result_returns_cancelled_result(
    worktree: Any,
    git_repo: Path,
    git_service: GitService,
    tmp_path: Path,
) -> None:
    holder: list[ClaudeAgentSdkRuntime] = []
    script: list[Any] = [assistant_text("hi")]

    def client_factory(options: Any) -> _CancelAfterQueryClient:
        return _CancelAfterQueryClient(options, script, runtime=holder[0])

    runtime = _make_runtime(
        tmp_path,
        git_service,
        script=script,
        client_factory=client_factory,
    )
    holder.append(runtime)

    result = await runtime.run_task(_task(worktree, git_repo), RecordingSink())

    assert result.status == "cancelled"


async def test_run_task_without_result_uses_failed_or_budget_exceeded_both_ways(
    worktree: Any,
    git_repo: Path,
    git_service: GitService,
    tmp_path: Path,
) -> None:
    failed_runtime = _make_runtime_with_pid_file(
        tmp_path,
        git_service,
        script=[],
        budget_exhausted=_budget_false,
    )
    result = await failed_runtime.run_task(_task(worktree, git_repo), RecordingSink())
    assert result.status == "failed"
    assert result.error == "Claude Agent SDK ended without a result"

    budget_runtime = _make_runtime_with_pid_file(
        tmp_path,
        git_service,
        script=[],
        budget_exhausted=_budget_true,
    )
    result = await budget_runtime.run_task(_task(worktree, git_repo), RecordingSink())
    assert result.status == "budget_exceeded"
    assert result.error == "LLM budget exceeded"


async def test_mint_failure_skips_revoke_in_finally(
    worktree: Any,
    git_repo: Path,
    git_service: GitService,
    tmp_path: Path,
) -> None:
    async def boom(task: AgentTask) -> Any:
        del task
        raise RuntimeError("mint boom")

    runtime = _make_runtime(tmp_path, git_service, script=[], mint_token=boom)
    with pytest.raises(RuntimeError, match="mint boom"):
        await runtime.run_task(_task(worktree, git_repo), RecordingSink())


async def test_drain_messages_skips_non_text_blocks(
    worktree: Any,
    git_repo: Path,
    git_service: GitService,
    tmp_path: Path,
) -> None:
    runtime = _make_runtime(tmp_path, git_service, script=[])
    message = AssistantMessage(
        model="test-model",
        content=[ThinkingBlock(thinking="hidden", signature="sig")],
    )
    result = await runtime._drain_messages(FakeSdkClient(None, [message]), RecordingSink())
    assert result is None


async def test_drain_messages_skips_non_result_messages(
    worktree: Any,
    git_repo: Path,
    git_service: GitService,
    tmp_path: Path,
) -> None:
    runtime = _make_runtime(tmp_path, git_service, script=[])
    message = SystemMessage(subtype="init", data={})
    result = await runtime._drain_messages(FakeSdkClient(None, [message]), RecordingSink())
    assert result is None


def test_bundled_cli_missing_raises_file_not_found(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        claude_agent_sdk,
        "__file__",
        str(tmp_path / "missing" / "__init__.py"),
    )
    with pytest.raises(FileNotFoundError, match="bundled Claude Code CLI"):
        _bundled_cli()


def test_short_result_error_uses_result_and_stop_reason_fallback() -> None:
    message = ResultMessage(
        subtype="error_during_execution",
        duration_ms=1,
        duration_api_ms=1,
        is_error=True,
        num_turns=1,
        session_id="test-session",
        result="sdk result text",
    )
    assert _short_result_error(message) == "sdk result text"

    stopped = ResultMessage(
        subtype="success",
        duration_ms=1,
        duration_api_ms=1,
        is_error=False,
        num_turns=1,
        session_id="test-session",
        stop_reason="stop reason text",
    )
    assert _short_result_error(stopped) == "stop reason text"


async def test_default_token_and_budget_helpers_use_real_db(
    database: Database,
    worktree: Any,
    git_repo: Path,
    git_service: GitService,
    tmp_path: Path,
) -> None:
    workspace_id = "ws-budget"
    async with session_scope(database.sessions) as session:
        session.add(Workspace(id=workspace_id, name="Budget", slug="budget"))
        await session.flush()
        session.add(
            User(
                id="test-user",
                workspace_id=workspace_id,
                email="test@example.com",
                display_name="Test User",
            )
        )
        await session.flush()
        repos = ScopedRepo(session, Repository, workspace_id)
        repository = await repos.create(
            name="budget-repo",
            local_path=str(worktree.work_tree),
            created_by="test-user",
        )
        tickets = ScopedRepo(session, Ticket, workspace_id)
        ticket = await tickets.create(
            repo_id=repository.id,
            title="budget-ticket",
            created_by="test-user",
        )
        runs = ScopedRepo(session, Run, workspace_id)
        run = await runs.create(
            ticket_id=ticket.id,
            repo_id=repository.id,
            created_by="test-user",
            budget_usd=Decimal(0),
        )
        run_id = run.id

    runtime = _make_runtime(
        tmp_path,
        git_service,
        script=[],
        sessions=database.sessions,
    )
    task = _task(worktree, git_repo).model_copy(
        update={"run_id": run_id, "workspace_id": workspace_id}
    )

    minted = await runtime._mint_token_default(task)
    assert minted.token.get_secret_value().startswith("gaia_rt_")
    await runtime._revoke_token_default(task, minted.token_id)
    assert await runtime._budget_exhausted_default(task) is True
    assert await runtime._budget_exhausted_default(_task(worktree, git_repo)) is False
