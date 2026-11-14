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

from gaia.db.ids import uuid7
from gaia.domain.enums import AgentRole, EventType
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
from gaia.services.git import WorktreeLocation
from tests.git_repo import git

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
