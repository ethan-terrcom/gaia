"""Per-repository allowed commands: policy rules, suggestions, remember, and the agent's view."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from gaia.db.models import AuditLog, Repository, Run
from gaia.db.repo import AuditLogRepo, ScopedRepo
from gaia.domain.enums import ApprovalKind, ApprovalStatus
from gaia.domain.plans import PlanStep
from gaia.orchestrator.impl.steps import _extra_allow, render_step_instructions
from gaia.runtimes.approvals import ApprovalDecision
from gaia.runtimes.base import AgentTask
from gaia.security.commands import (
    MAX_ALLOWED_ENTRY_ITEMS,
    allowed_entry_problem,
    suggest_allowed_prefix,
)
from gaia.services.allowed_commands import (
    allowed_entries,
    extra_allow_for,
    load_extra_allow,
    remember,
)
from gaia.tools import shell
from gaia.tools.registry import ToolRegistry
from tests.conftest import Api, Database
from tests.test_api_decide import _add_approval, _repo_and_run_id, run_api
from tests.tool_fakes import FakeCommandRunner, make_proc_result, make_tool_context

__all__ = ["run_api"]  # the fixture is imported for this module's tests

pytestmark = pytest.mark.anyio


# --- the policy's rules --------------------------------------------------------


@pytest.mark.parametrize(
    ("entry", "problem"),
    [
        ([], "cannot be empty"),
        (["make"] * (MAX_ALLOWED_ENTRY_ITEMS + 1), "at most"),
        (["make", "te\x00st"], "plain words"),
        (["frontend/node_modules/.bin/vitest", "run"], "is a path"),
        (["git", "status"], "always blocked"),
        (["rm", "-rf", "build"], "always blocked"),
        (["uv", "run", "rm", "x"], "always blocked"),
        (["npx", "vite", "build"], "cannot be allowed"),
        (["npm"], "a bare 'npm'"),
        (["uv", "run", "python"], "a bare 'python'"),
        (["node", "-e"], "any code"),
        (["bash", "-c", "make"], "any code"),
        (["python3", "-c", "print(1)"], "any code"),
    ],
)
def test_allowed_entry_problem_refuses(entry: list[str], problem: str) -> None:
    found = allowed_entry_problem(entry)

    assert found is not None
    assert problem in found


@pytest.mark.parametrize(
    "entry",
    [
        ["npm", "test"],
        ["npm", "--prefix", "frontend", "run", "build"],
        ["python3", "-m", "unittest"],
        ["make", "lint"],
        ["npx", "--no-install", "vitest", "run"],
    ],
)
def test_allowed_entry_problem_accepts(entry: list[str]) -> None:
    assert allowed_entry_problem(entry) is None


@pytest.mark.parametrize(
    ("argv", "prefix"),
    [
        (["python3", "-m", "unittest", "test_greet.py", "-v"], ("python3", "-m", "unittest")),
        (["npm", "test", "--", "src/x.test.tsx"], ("npm", "test")),
        (
            ["npm", "--prefix", "frontend", "test", "--", "x"],
            ("npm", "--prefix", "frontend", "test"),
        ),
        (
            ["npm", "--prefix", "frontend", "run", "lint"],
            ("npm", "--prefix", "frontend", "run", "lint"),
        ),
        (["cargo", "test", "--release"], ("cargo", "test")),
        (["make", "check", "."], ("make", "check")),
        (["make", "test", "PYTEST_ARGS=-k"], ("make", "test")),
        (["node", "-e", "console.log(1)"], None),
        (["frontend/node_modules/.bin/vitest", "run"], None),
        (["npx", "vite", "build", "--outDir", "/tmp/x"], None),
        (["npm", "--version"], None),
        ([], None),
        (["npm", 3], None),
    ],
)
def test_suggest_allowed_prefix(argv: list[Any], prefix: tuple[str, ...] | None) -> None:
    assert suggest_allowed_prefix(argv) == prefix


# --- the service -----------------------------------------------------------------


def _repository(**fields: Any) -> Repository:
    return Repository(name="r", local_path="/r", **fields)


def test_allowed_entries_skip_malformed_or_refused_rows() -> None:
    repository = _repository(
        test_command=["make", "test"],
        allowed_commands=[["npm", "test"], ["npm"], "make lint", ["make", 3], ["make", "lint"]],
    )

    assert allowed_entries(repository) == (("npm", "test"), ("make", "lint"))
    assert extra_allow_for(repository) == (("make", "test"), ("npm", "test"), ("make", "lint"))
    assert extra_allow_for(_repository()) == ()


async def _run_repo(api: Api, database: Database) -> tuple[str, str, str]:
    workspace_id, run_id = await _repo_and_run_id(api, database)
    async with database.sessions() as session:
        run = await ScopedRepo(session, Run, workspace_id).get(run_id)
        assert run is not None
        return workspace_id, run_id, run.repo_id


async def test_remember_appends_once_and_load_reads_it_live(
    run_api: Api, database: Database
) -> None:
    workspace_id, run_id, repo_id = await _run_repo(run_api, database)

    assert await load_extra_allow(database.sessions, workspace_id, run_id) == ()
    assert await remember(database.sessions, workspace_id, repo_id, ["npm", "test"]) is True
    assert await remember(database.sessions, workspace_id, repo_id, ["npm", "test"]) is False

    assert await load_extra_allow(database.sessions, workspace_id, run_id) == (("npm", "test"),)
    assert await load_extra_allow(database.sessions, workspace_id, "no-such-run") == ()


async def test_remember_refuses_bad_entries_and_unknown_repositories(
    run_api: Api, database: Database
) -> None:
    workspace_id, _, repo_id = await _run_repo(run_api, database)

    with pytest.raises(ValueError, match="bare 'npm'"):
        await remember(database.sessions, workspace_id, repo_id, ["npm"])
    with pytest.raises(LookupError):
        await remember(database.sessions, workspace_id, "no-such-repo", ["npm", "test"])


async def test_load_extra_allow_without_a_repository_row(
    run_api: Api, database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace_id, run_id, _ = await _run_repo(run_api, database)
    original_get = ScopedRepo.get

    async def get_without_repositories(self: ScopedRepo[Any], row_id: str) -> Any:
        if self.model is Repository:
            return None
        return await original_get(self, row_id)

    monkeypatch.setattr(ScopedRepo, "get", get_without_repositories)

    assert await load_extra_allow(database.sessions, workspace_id, run_id) == ()


# --- the API ---------------------------------------------------------------------


async def _register(api: Api, git_repo: Path) -> str:
    response = await api.client.post(
        "/api/v1/repositories",
        json={"local_path": str(git_repo), "test_command": ["make", "test"]},
        headers=api.auth,
    )
    assert response.status_code == 201
    assert response.json()["allowed_commands"] == []
    return str(response.json()["id"])


async def test_patch_allowed_commands_replaces_the_list_without_duplicates(
    api: Api, git_repo: Path
) -> None:
    repository_id = await _register(api, git_repo)

    response = await api.client.patch(
        f"/api/v1/repositories/{repository_id}",
        json={"allowed_commands": [["npm", "test"], ["make", "lint"], ["npm", "test"]]},
        headers=api.auth,
    )

    assert response.status_code == 200
    assert response.json()["allowed_commands"] == [["npm", "test"], ["make", "lint"]]
    assert response.json()["test_command"] == ["make", "test"]

    cleared = await api.client.patch(
        f"/api/v1/repositories/{repository_id}",
        json={"allowed_commands": None},
        headers=api.auth,
    )
    assert cleared.json()["allowed_commands"] == []


@pytest.mark.parametrize(
    ("entries", "message"),
    [
        ([["npm"]], "bare 'npm'"),
        ([["git", "push"]], "always blocked"),
        ([["make", str(i)] for i in range(101)], "at most 100"),
    ],
)
async def test_patch_allowed_commands_refuses_bad_entries(
    api: Api, git_repo: Path, entries: list[list[str]], message: str
) -> None:
    repository_id = await _register(api, git_repo)

    response = await api.client.patch(
        f"/api/v1/repositories/{repository_id}",
        json={"allowed_commands": entries},
        headers=api.auth,
    )

    assert response.status_code == 422
    assert message in response.text


async def test_command_suggestions_come_from_approved_commands(
    run_api: Api, database: Database
) -> None:
    workspace_id, run_id, repo_id = await _run_repo(run_api, database)
    approved = [
        ["npm", "--prefix", "frontend", "test", "--", "a.test.ts"],
        ["npm", "--prefix", "frontend", "test"],
        ["npm", "--prefix", "frontend", "run", "build"],
        ["node", "-e", "1"],  # cannot be remembered
        ["pytest", "-q"],  # already allowed
        ["npm", "--prefix", "frontend", "run", "lint"],  # already an allowed command
    ]
    for index, argv in enumerate(approved):
        await _add_approval(
            database,
            workspace_id,
            run_id,
            idempotency_key=f"cmd:s{index}",
            kind=ApprovalKind.COMMAND,
            payload={"argv": argv},
            status=ApprovalStatus.APPROVED,
        )
    others: list[dict[str, Any]] = [{"argv": ["make", "x"]}, {"argv": "make x"}, {}]
    for index, payload in enumerate(others):
        status = ApprovalStatus.REJECTED if index == 0 else ApprovalStatus.APPROVED
        await _add_approval(
            database,
            workspace_id,
            run_id,
            idempotency_key=f"cmd:o{index}",
            kind=ApprovalKind.COMMAND,
            payload=payload,
            status=status,
        )
    await remember(
        database.sessions, workspace_id, repo_id, ["npm", "--prefix", "frontend", "run", "lint"]
    )

    response = await run_api.client.get(
        f"/api/v1/repositories/{repo_id}/command-suggestions", headers=run_api.auth
    )

    assert response.status_code == 200
    assert response.json()["items"] == [
        {"argv": ["npm", "--prefix", "frontend", "test"], "times_approved": 2},
        {"argv": ["npm", "--prefix", "frontend", "run", "build"], "times_approved": 1},
    ]
    missing = await run_api.client.get(
        "/api/v1/repositories/nope/command-suggestions", headers=run_api.auth
    )
    assert missing.status_code == 404


async def test_approve_and_remember_adds_the_prefix_and_audits(
    run_api: Api, database: Database
) -> None:
    workspace_id, run_id, repo_id = await _run_repo(run_api, database)
    approval_id = await _add_approval(
        database,
        workspace_id,
        run_id,
        idempotency_key="cmd:r1",
        kind=ApprovalKind.COMMAND,
        payload={"argv": ["npm", "--prefix", "frontend", "test", "--", "a.test.ts"]},
    )

    response = await run_api.client.post(
        f"/api/v1/approvals/{approval_id}/decide",
        json={"decision": "approve", "remember": True},
        headers=run_api.auth,
    )

    assert response.status_code == 200
    assert response.json()["status"] == "approved"
    assert await load_extra_allow(database.sessions, workspace_id, run_id) == (
        ("npm", "--prefix", "frontend", "test"),
    )
    async with database.sessions() as session:
        audits = AuditLogRepo(session, AuditLog, workspace_id)
        allow_rows = await audits.list(action="repository.allow_command")
        decide_rows = await audits.list(action="approval.decide")
    assert [row.details_json["argv"] for row in allow_rows] == [
        ["npm", "--prefix", "frontend", "test"]
    ]
    assert allow_rows[0].target == repo_id
    assert decide_rows[-1].details_json["remembered"] == ["npm", "--prefix", "frontend", "test"]


@pytest.mark.parametrize(
    ("kind", "decision", "payload", "message"),
    [
        (ApprovalKind.COMMAND, "reject", {"argv": ["npm", "test"]}, "only valid when approving"),
        (ApprovalKind.PLAN, "approve", {}, "only valid when approving"),
        (ApprovalKind.COMMAND, "approve", {"argv": ["node", "-e", "1"]}, "cannot be added"),
        (ApprovalKind.COMMAND, "approve", {"argv": "npm test"}, "cannot be added"),
    ],
)
async def test_remember_is_refused_where_it_cannot_apply(
    run_api: Api,
    database: Database,
    kind: ApprovalKind,
    decision: str,
    payload: dict[str, Any],
    message: str,
) -> None:
    workspace_id, run_id = await _repo_and_run_id(run_api, database)
    approval_id = await _add_approval(
        database,
        workspace_id,
        run_id,
        idempotency_key="cmd:bad",
        kind=kind,
        payload=payload,
    )

    response = await run_api.client.post(
        f"/api/v1/approvals/{approval_id}/decide",
        json={"decision": decision, "remember": True},
        headers=run_api.auth,
    )

    assert response.status_code == 422
    assert message in response.text
    assert await load_extra_allow(database.sessions, workspace_id, run_id) == ()


# --- what the agent sees ---------------------------------------------------------


def _step() -> PlanStep:
    return PlanStep(
        title="Fix the button",
        instructions="Change the colour.",
        files_likely_touched=[],
        acceptance=[],
        test_command=None,
    )


def test_step_instructions_list_the_repository_commands() -> None:
    repository = _repository(
        setup_command=["make", "setup-worktree"],
        test_command=["make", "test"],
        allowed_commands=[["npm", "--prefix", "frontend", "test"], ["npm"]],
    )

    rendered = render_step_instructions(_step(), attempt=1, feedback=None, repository=repository)

    assert "## Repository commands" in rendered
    assert "- `make test` (the repository's test command)" in rendered
    assert "- `npm --prefix frontend test`" in rendered
    assert "- `npm`\n" not in rendered
    assert "Setup (`make setup-worktree`) has already run" in rendered
    # The demo model reads a "Test command:" line as the step's test command.
    assert "Test command:" not in rendered
    assert _extra_allow(repository) == (("make", "test"), ("npm", "--prefix", "frontend", "test"))


def test_step_instructions_say_when_no_commands_are_registered() -> None:
    rendered = render_step_instructions(_step(), attempt=1, feedback=None, repository=_repository())

    assert "- (none registered)" in rendered
    assert "Setup (" not in rendered
    assert "## Repository commands" not in render_step_instructions(
        _step(), attempt=1, feedback=None
    )


class _RecordingBroker:
    """Approve every request and keep its payload."""

    def __init__(self) -> None:
        self.payloads: list[Mapping[str, Any]] = []

    async def request(
        self, task: AgentTask, *, kind: ApprovalKind, payload: Mapping[str, Any], timeout_s: float
    ) -> ApprovalDecision:
        del task, kind, timeout_s
        self.payloads.append(payload)
        return ApprovalDecision(True, "ok")


@pytest.fixture
def registry() -> ToolRegistry:
    built = ToolRegistry()
    built.register(shell.run_command)
    return built


async def test_run_command_reads_allowed_commands_live(
    registry: ToolRegistry, tmp_path: Path
) -> None:
    worktree = tmp_path / "wt"
    worktree.mkdir()
    allowed: list[tuple[str, ...]] = []

    async def source() -> tuple[tuple[str, ...], ...]:
        return tuple(allowed)

    broker = _RecordingBroker()
    runner = FakeCommandRunner(
        [
            make_proc_result(["npm", "test", "--", "a.ts"], cwd=worktree),
            make_proc_result(["npm", "test", "--", "a.ts"], cwd=worktree),
        ]
    )
    ctx = make_tool_context(worktree, commands=runner, approvals=broker, extra_allow_source=source)

    first = await registry.call(
        "run_command", {"argv": ["npm", "test", "--", "a.ts"]}, ctx, call_id="call-1"
    )
    allowed.append(("npm", "test"))
    second = await registry.call(
        "run_command", {"argv": ["npm", "test", "--", "a.ts"]}, ctx, call_id="call-2"
    )

    assert first.decision == "ask_approved"
    assert broker.payloads[0]["remember_prefix"] == ["npm", "test"]
    assert second.decision == "allow"
    assert len(broker.payloads) == 1


async def test_a_command_that_cannot_be_remembered_has_no_prefix(
    registry: ToolRegistry, tmp_path: Path
) -> None:
    worktree = tmp_path / "wt"
    worktree.mkdir()
    broker = _RecordingBroker()
    runner = FakeCommandRunner([make_proc_result(["node", "-e", "1"], cwd=worktree)])
    ctx = make_tool_context(worktree, commands=runner, approvals=broker)

    await registry.call("run_command", {"argv": ["node", "-e", "1"]}, ctx, call_id="call-3")

    assert broker.payloads[0]["remember_prefix"] is None
