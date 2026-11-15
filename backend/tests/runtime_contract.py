"""Shared helpers for the runtime contract suite (doc 10a).

The tests in ``test_runtime_contract.py`` parametrise over the in-process
:class:`~gaia.runtimes.fake.FakeRuntime` and the subprocess
:class:`~gaia.runtimes.fake.FakeProcessRuntime`. These helpers keep the two
scripts and the worktree setup in one place.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from gaia.config.settings import Settings
from gaia.db.ids import uuid7
from gaia.domain.enums import AgentRole, ApprovalKind, EventType
from gaia.runtimes.approvals import ApprovalBroker
from gaia.runtimes.base import AgentRuntime, AgentTask
from gaia.runtimes.fake import (
    Emit,
    Fail,
    FakeAction,
    FakeRuntime,
    Finish,
    RequestApproval,
    Sleep,
    WriteFile,
)
from gaia.runtimes.orphans import OrphanRegistry
from gaia.security.audit import InMemoryAuditSink
from gaia.security.commands import CommandPolicy
from gaia.services.git import GitService, WorktreeLocation
from gaia.tools.registry import Tool, ToolContext, ToolInput, ToolRegistry, ToolResult
from tests.git_repo import git
from tests.sdk_fakes import (
    FakeSdkClient,
    SdkBlock,
    SdkToolCall,
    assistant_text,
    error_result,
    success_result,
)

#: A planted provider key with an obvious fake value. The env-scrub test proves
#: it never reaches an agent environment even when it is in Gaia's environment.
PLANTED_ANTHROPIC_KEY = "sk-ant-planted-0000000000000000000000"


class RecordingSink:
    """An :class:`EventSink` that records events in memory, for contract tests."""

    def __init__(self) -> None:
        self.events: list[tuple[EventType, dict[str, Any]]] = []

    async def emit(self, type: EventType, payload: Mapping[str, Any]) -> None:
        self.events.append((type, dict(payload)))


@dataclass(frozen=True)
class RuntimeFactory:
    """A named way to build one runtime for the contract tests."""

    name: str
    build: Callable[..., AgentRuntime]


def make_task(
    worktree: WorktreeLocation,
    *,
    base_sha: str,
    instructions: str = "do the task",
    timeout_s: float = 30.0,
) -> AgentTask:
    """Build a valid :class:`AgentTask` around a worktree."""
    return AgentTask(
        task_id=uuid7(),
        run_id=uuid7(),
        run_step_id=None,
        workspace_id="ws-contract",
        agent_id=None,
        role=AgentRole.JUNIOR,
        instructions=instructions,
        worktree=worktree,
        base_sha=base_sha,
        timeout_s=timeout_s,
    )


def make_worktree_location(repo: Path, tmp_path: Path) -> WorktreeLocation:
    """Create a linked worktree of ``repo`` and return its trusted location."""
    work_tree = tmp_path / "worktree"
    git(repo, "worktree", "add", "-q", "--detach", str(work_tree), "HEAD")
    git_dir = git(work_tree, "rev-parse", "--git-dir").strip()
    git_dir_path = Path(git_dir)
    if not git_dir_path.is_absolute():
        git_dir_path = work_tree / git_dir_path
    return WorktreeLocation(work_tree=work_tree, git_dir=git_dir_path)


def base_sha(repo: Path) -> str:
    """The commit the contract worktree starts from."""
    return git(repo, "rev-parse", "HEAD").strip()


def write_process_script(tmp_path: Path, actions: Sequence[Mapping[str, Any]]) -> Path:
    """Write a JSON script for :class:`FakeProcessRuntime`."""
    path = tmp_path / "fake-agent-script.json"
    path.write_text(json.dumps(list(actions), sort_keys=True))
    return path


def to_json_actions(actions: Sequence[FakeAction]) -> list[dict[str, Any]]:
    """Convert :class:`FakeAction` values to fake-agent JSON actions."""
    json_actions: list[dict[str, Any]] = []
    for action in actions:
        if isinstance(action, Emit):
            json_actions.append(
                {
                    "type": "emit",
                    "event_type": action.type.value,
                    "payload": dict(action.payload),
                }
            )
        elif isinstance(action, WriteFile):
            json_actions.append({"type": "write", "path": action.path, "content": action.content})
        elif isinstance(action, RequestApproval):
            json_actions.append({"type": "approval", "argv": list(action.argv)})
        elif isinstance(action, Sleep):
            json_actions.append({"type": "sleep", "seconds": action.seconds})
        elif isinstance(action, Fail):
            json_actions.append({"type": "fail", "message": action.message})
        elif isinstance(action, Finish):
            json_actions.append(
                {
                    "type": "finish",
                    "summary": action.summary,
                    "structured_output": action.structured_output,
                    "files_changed": list(action.files_changed),
                }
            )
    return json_actions


class _UnusedCommandRunner:
    """The contract SDK runtime never runs a command directly."""

    async def run(
        self,
        argv: Sequence[str],
        *,
        cwd: Path,
        timeout_s: float,
        task_dir: Path,
    ) -> Any:
        del argv, cwd, timeout_s, task_dir
        raise AssertionError("run_command must not be used by the claude_sdk contract script")


class _RequestApprovalInput(ToolInput):
    argv: list[str]


async def _request_approval_handler(input: Any, ctx: ToolContext) -> ToolResult:
    """A tiny contract tool that routes straight to the shared approval broker."""
    model = _RequestApprovalInput.model_validate(input)
    decision = await ctx.approvals.request(
        ctx.task,
        kind=ApprovalKind.COMMAND,
        payload={"argv": tuple(model.argv)},
        timeout_s=ctx.approval_timeout_s,
    )
    if decision.approved:
        return ToolResult(True, "approved", decision="allow")
    return ToolResult(False, f"denied: {decision.reason}", decision="ask_rejected")


def _contract_registry() -> ToolRegistry:
    """Build the small registry used by the claude_sdk contract runtime."""
    from gaia.tools.fs import write_file

    registry = ToolRegistry()
    registry.register(write_file)
    registry.register(
        Tool(
            name="request_approval",
            description="Ask the contract approval broker for a decision.",
            input_model=_RequestApprovalInput,
            handler=_request_approval_handler,
            mutates=False,
        )
    )
    return registry


def build_claude_sdk_runtime(
    actions: Sequence[FakeAction],
    broker: ApprovalBroker,
    tmp_path: Path,
    *,
    git_service: GitService,
    orphans: OrphanRegistry | None = None,
) -> AgentRuntime:
    """Build the Claude Agent SDK runtime for the contract suite."""
    from gaia.runtimes.claude_agent_sdk import ClaudeAgentSdkRuntime

    settings = Settings(
        data_dir=tmp_path / "gaia-data",
        srt_path=tmp_path / "srt",
    )
    # ``find_srt`` only checks that this is an executable file in contract tests;
    # it is never launched.
    srt_path = tmp_path / "srt"
    srt_path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    srt_path.chmod(0o700)

    async def context_factory(task: AgentTask, events: Any, task_dir: Path) -> ToolContext:
        return ToolContext(
            task=task,
            git=git_service,
            policy=CommandPolicy.from_default(),
            approvals=broker,
            audit=InMemoryAuditSink(),
            events=events,
            commands=_UnusedCommandRunner(),
            task_dir=task_dir,
            actor="contract-agent",
            approval_timeout_s=600,
            rg_path=None,
        )

    script = _sdk_script(actions)
    runtime: ClaudeAgentSdkRuntime | None = None

    def client_factory(options: Any) -> FakeSdkClient:
        assert runtime is not None
        return FakeSdkClient(options, script, runtime=runtime)

    runtime = ClaudeAgentSdkRuntime(
        registry=_contract_registry(),
        settings=settings,
        sessions=None,
        context_factory=context_factory,
        model="claude-opus-5-5",
        client_factory=client_factory,
        orphans=orphans,
        budget_exhausted=lambda task: _false(task),
        mint_token=lambda task: _fake_mint(task),
        revoke_token=lambda task, token_id: _fake_revoke(task, token_id),
    )
    return runtime


async def _false(task: AgentTask) -> bool:
    del task
    return False


class _FakeMinted:
    def __init__(self) -> None:
        from pydantic import SecretStr

        self.token_id = uuid7()
        self.token = SecretStr("gaia_rt_contract-token")


async def _fake_mint(task: AgentTask) -> Any:
    del task
    return _FakeMinted()


async def _fake_revoke(task: AgentTask, token_id: str) -> None:
    del task, token_id


def _sdk_script(actions: Sequence[FakeAction]) -> list[Any]:
    """Convert contract fake actions to an SDK client script."""
    script: list[Any] = []
    for action in actions:
        if isinstance(action, WriteFile):
            script.append(
                SdkToolCall(
                    "write_file",
                    {"path": action.path, "content": action.content},
                )
            )
        elif isinstance(action, Finish):
            script.append(
                SdkToolCall(
                    "finish",
                    {
                        "summary": action.summary,
                        "tests_run": [],
                        "tests_passed": True,
                        "concerns": [],
                    },
                )
            )
            script.append(success_result())
        elif isinstance(action, Sleep):
            script.append(SdkBlock())
        elif isinstance(action, Emit):
            if action.type is EventType.AGENT_MESSAGE:
                text = str(action.payload.get("content") or action.payload.get("message") or "")
                script.append(assistant_text(text))
            elif action.type is EventType.AGENT_TOOL_CALL:
                script.append(
                    SdkToolCall(
                        "write_file",
                        {"path": f"contract-{uuid7()}.txt", "content": "contract\n"},
                    )
                )
        elif isinstance(action, RequestApproval):
            script.append(
                SdkToolCall(
                    "request_approval",
                    {"argv": list(action.argv)},
                )
            )
        elif isinstance(action, Fail):
            script.append(error_result(action.message))
    return script


def build_fake_runtime(
    actions: Sequence[FakeAction],
    broker: ApprovalBroker,
    orphans: OrphanRegistry | None = None,
) -> AgentRuntime:
    """Build the in-process fake runtime."""
    del orphans
    return FakeRuntime(actions, broker)


def build_fake_process_runtime(
    actions: Sequence[Mapping[str, Any]],
    broker: ApprovalBroker,
    tmp_path: Path,
    orphans: OrphanRegistry | None = None,
) -> AgentRuntime:
    """Build the subprocess fake runtime from JSON actions."""
    from gaia.runtimes.fake import FakeProcessRuntime

    return FakeProcessRuntime(
        write_process_script(tmp_path, actions),
        broker,
        orphans=orphans,
    )


async def wait_for_pending(
    broker: Any,
    *,
    timeout_s: float = 5.0,
    exclude: set[str] | None = None,
) -> str:
    """Poll ``broker.pending()`` until a new id appears, then return it.

    The loop is bounded twice: by ``asyncio.timeout`` and by the finite range.
    """
    excluded = exclude or set()
    async with asyncio.timeout(timeout_s):
        for _ in range(10_000):
            for approval_id in broker.pending():
                if approval_id not in excluded:
                    return str(approval_id)
            await asyncio.sleep(0.01)
    raise AssertionError("no approval request arrived")


async def wait_until(condition: Callable[[], bool], *, timeout_s: float = 5.0) -> None:
    """Poll ``condition`` until it is true, with a bounded wait."""
    async with asyncio.timeout(timeout_s):
        for _ in range(10_000):
            if condition():
                return
            await asyncio.sleep(0.01)
    raise AssertionError("condition never became true")
