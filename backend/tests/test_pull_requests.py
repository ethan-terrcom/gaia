"""Doc 15a ``PullRequestService``: open_pr, merge, build_pr_body."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncSession

import gaia.services.pull_requests as pr_module
from gaia.config.secrets import InMemorySecretStore, MissingSecretError, SecretName
from gaia.config.settings import Settings
from gaia.db.engine import session_scope
from gaia.db.models import Approval, Event, PullRequest, Repository, Run, RunStep, Ticket, Worktree
from gaia.db.repo import EventRepo, ScopedRepo
from gaia.domain.enums import ApprovalKind, ApprovalStatus, CiStatus, EventType, PullRequestState
from gaia.services.events import EventBus
from gaia.services.git import GitService, WorktreeLocation
from gaia.services.github import (
    GitHubClient,
    GitHubSetupError,
    MergeBlocked,
    parse_github_remote,
)
from gaia.services.pull_requests import (
    PullRequestService,
    _parse_repo_slug,
    _plan_summary,
    build_pr_body,
)
from tests.db_builders import make_chain
from tests.github_fakes import GitHubFake, json_response

TOKEN = "github_pat_planted_0000000000000000000000000000000000"
SLUG = parse_github_remote("https://github.com/acme/widgets.git")
PUSH_URL = SLUG.https_url


def _pr_payload(number: int = 7, *, state: str = "open", merged: bool = False) -> dict[str, object]:
    return {
        "number": number,
        "html_url": f"https://github.com/acme/widgets/pull/{number}",
        "state": state,
        "draft": True,
        "node_id": f"PR_node_{number}",
        "merged_at": "2026-11-14T00:00:00Z" if merged else None,
        "head": {"sha": "a" * 40, "ref": "gaia/gh"},
        "base": {"sha": "b" * 40, "ref": "main"},
    }


def _repo_payload(*, push: bool = True) -> dict[str, object]:
    return {
        "default_branch": "main",
        "private": True,
        "permissions": {"push": push, "pull": True},
    }


def _client_factory(fake: GitHubFake):
    def factory(token: SecretStr) -> GitHubClient:
        return GitHubClient(
            token,
            base_url="https://github.test",
            transport=httpx.MockTransport(fake),
        )

    return factory


async def _noop_push(
    location: WorktreeLocation,
    *,
    url: str,
    branch: str,
    token: SecretStr,
    timeout_s: float = 120,
) -> None:
    """A stand-in for ``GitService.push`` that records nothing."""
    return None


async def _validate_repo(path: Path | str) -> Path:
    """A stand-in for ``GitService.validate_repo``: no filesystem needed."""
    return Path(path)


async def _make_chain_for_github(
    session: AsyncSession, git_settings: Settings, *, slug: str = "gh"
):
    """Build a chain and make its repo/worktree point at GitHub and the worktree root."""
    chain = await make_chain(session, slug=slug)
    workspace_id = chain.workspace.id
    await ScopedRepo(session, Repository, workspace_id).update(
        chain.repository.id, remote_url=PUSH_URL
    )
    worktree_path = git_settings.worktrees_root / "repo" / "wt"
    worktree_path.parent.mkdir(parents=True, exist_ok=True)
    await ScopedRepo(session, Worktree, workspace_id).update(
        chain.worktree.id,
        path=str(worktree_path),
        git_dir=str(worktree_path / "gitdir"),
    )
    return chain


def _fake_with_token_check_success(fake: GitHubFake) -> GitHubFake:
    """Add the common token-check success routes to ``fake``."""
    fake.add_json("GET", "/repos/acme/widgets", _repo_payload())
    fake.add_json("GET", "/repos/acme/widgets/issues", [])
    fake.add_json("GET", "/repos/acme/widgets/commits/main/status", {})
    fake.add_json("GET", "/repos/acme/widgets/commits/main/check-runs", {})
    return fake


async def _opened_row(session: AsyncSession, workspace_id: str, run_id: str) -> PullRequest:
    rows = await ScopedRepo(session, PullRequest, workspace_id).list(run_id=run_id)
    assert len(rows) == 1
    return rows[0]


# ----------------------------------------------------------------- open_pr --
async def test_open_pr_creates_a_draft_pr_and_writes_row_and_event(
    database,
    git_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = GitHubFake()
    _fake_with_token_check_success(fake)
    pull_count = 0

    def pulls(request: httpx.Request) -> httpx.Response:
        nonlocal pull_count
        pull_count += 1
        # First call is check_token's read check, second is find_open_pr before
        # creation; after that the PR exists.
        if pull_count <= 2:
            return json_response(200, [])
        return json_response(200, [_pr_payload()])

    fake.add_dynamic("GET", "/repos/acme/widgets/pulls", pulls)
    fake.add_json("POST", "/repos/acme/widgets/pulls", _pr_payload())

    git = GitService(git_settings)
    monkeypatch.setattr(git, "validate_repo", _validate_repo)
    pushed: list[tuple[str, str, float]] = []

    async def fake_push(
        location: WorktreeLocation,
        *,
        url: str,
        branch: str,
        token: SecretStr,
        timeout_s: float = 120,
    ) -> None:
        pushed.append((url, branch, timeout_s))

    monkeypatch.setattr(git, "push", fake_push)
    store = InMemorySecretStore()
    store.set(SecretName.GITHUB_TOKEN, TOKEN)
    bus = EventBus(database.sessions)
    service = PullRequestService(
        sessions=database.sessions,
        git=git,
        secrets=store,
        bus=bus,
        client_factory=_client_factory(fake),
    )

    async with session_scope(database.sessions) as session:
        chain = await _make_chain_for_github(session, git_settings)
        await ScopedRepo(session, Ticket, chain.workspace.id).update(
            chain.ticket.id, title="Add @boss endpoint"
        )
        await ScopedRepo(session, Worktree, chain.workspace.id).update(
            chain.worktree.id, branch="gaia/gh"
        )
        await ScopedRepo(session, RunStep, chain.workspace.id).update(
            chain.step.id,
            title="Add @boss endpoint",
            status="completed",
            attempts=2,
            result_summary="@boss looked good",
        )
        await ScopedRepo(session, Approval, chain.workspace.id).create(
            run_id=chain.run.id,
            idempotency_key="plan:0:0",
            kind=ApprovalKind.PLAN.value,
            payload_json={"plan": {"summary": "Ship @boss widgets"}},
            status=ApprovalStatus.APPROVED.value,
        )
        await ScopedRepo(session, Run, chain.workspace.id).update(
            chain.run.id, cost_usd=Decimal("12.34")
        )
        run_id = chain.run.id
        workspace_id = chain.workspace.id

    number = await service.open_pr(run_id)

    assert number == 7
    assert pushed == [(PUSH_URL, "gaia/gh", 120)]
    post = [call for call in fake.calls if call.method == "POST"]
    assert len(post) == 1
    body = json.loads(post[0].content)
    assert body["draft"] is True
    assert body["head"] == "gaia/gh"
    assert body["base"] == "main"
    assert body["title"] == "[gaia] Add @boss endpoint"
    assert "Ship @\u200dboss widgets" in body["body"]
    assert "@boss" not in body["body"]
    assert "Closes" not in body["body"]

    async with database.sessions() as session:
        row = await _opened_row(session, workspace_id, run_id)
        assert row.number == 7
        assert row.url == "https://github.com/acme/widgets/pull/7"
        assert row.state == "open"
        assert row.ci_status == CiStatus.PENDING.value
        events = await EventRepo(session, Event, workspace_id).list_for_run(run_id)
        assert [event.type for event in events] == [EventType.PR_OPENED.value]


async def test_open_pr_is_idempotent(
    database,
    git_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = GitHubFake()
    _fake_with_token_check_success(fake)
    pull_count = 0

    def pulls(request: httpx.Request) -> httpx.Response:
        nonlocal pull_count
        pull_count += 1
        if pull_count <= 2:
            return json_response(200, [])
        return json_response(200, [_pr_payload()])

    fake.add_dynamic("GET", "/repos/acme/widgets/pulls", pulls)
    fake.add_json("POST", "/repos/acme/widgets/pulls", _pr_payload())

    git = GitService(git_settings)
    monkeypatch.setattr(git, "push", _noop_push)
    monkeypatch.setattr(git, "validate_repo", _validate_repo)
    store = InMemorySecretStore()
    store.set(SecretName.GITHUB_TOKEN, TOKEN)
    bus = EventBus(database.sessions)
    service = PullRequestService(
        sessions=database.sessions,
        git=git,
        secrets=store,
        bus=bus,
        client_factory=_client_factory(fake),
    )

    async with session_scope(database.sessions) as session:
        chain = await _make_chain_for_github(session, git_settings)
        run_id = chain.run.id
        workspace_id = chain.workspace.id

    assert await service.open_pr(run_id) == 7
    assert await service.open_pr(run_id) == 7

    assert len([call for call in fake.calls if call.method == "POST"]) == 1
    async with database.sessions() as session:
        row = await _opened_row(session, workspace_id, run_id)
        assert row.number == 7
        events = await EventRepo(session, Event, workspace_id).list_for_run(run_id)
        assert [event.type for event in events] == [EventType.PR_OPENED.value]


async def test_open_pr_no_remote_is_a_setup_error(
    database,
    git_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = GitHubFake()
    git = GitService(git_settings)
    monkeypatch.setattr(git, "push", _noop_push)
    store = InMemorySecretStore()
    store.set(SecretName.GITHUB_TOKEN, TOKEN)
    service = PullRequestService(
        sessions=database.sessions,
        git=git,
        secrets=store,
        bus=EventBus(database.sessions),
        client_factory=_client_factory(fake),
    )

    async with session_scope(database.sessions) as session:
        chain = await _make_chain_for_github(session, git_settings)
        await ScopedRepo(session, Repository, chain.workspace.id).update(
            chain.repository.id, remote_url=None
        )
        run_id = chain.run.id

    with pytest.raises(GitHubSetupError, match="no GitHub remote"):
        await service.open_pr(run_id)

    assert fake.calls == []


async def test_open_pr_missing_permission_names_it(
    database,
    git_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = GitHubFake()
    fake.add_json("GET", "/repos/acme/widgets", _repo_payload())
    fake.add_json("GET", "/repos/acme/widgets/pulls", [])
    fake.add_json(
        "GET",
        "/repos/acme/widgets/issues",
        {"message": "Forbidden"},
        status_code=403,
    )
    fake.add_json("GET", "/repos/acme/widgets/commits/main/status", {})
    fake.add_json("GET", "/repos/acme/widgets/commits/main/check-runs", {})

    git = GitService(git_settings)
    monkeypatch.setattr(git, "validate_repo", _validate_repo)
    monkeypatch.setattr(git, "push", _noop_push)
    store = InMemorySecretStore()
    store.set(SecretName.GITHUB_TOKEN, TOKEN)
    service = PullRequestService(
        sessions=database.sessions,
        git=git,
        secrets=store,
        bus=EventBus(database.sessions),
        client_factory=_client_factory(fake),
    )

    async with session_scope(database.sessions) as session:
        chain = await _make_chain_for_github(session, git_settings)
        run_id = chain.run.id

    with pytest.raises(GitHubSetupError, match="Issues R"):
        await service.open_pr(run_id)

    assert [call for call in fake.calls if call.method == "POST"] == []


async def test_open_pr_no_token_raises_missing_secret(
    database,
    git_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = GitHubFake()
    git = GitService(git_settings)
    monkeypatch.setattr(git, "validate_repo", _validate_repo)
    monkeypatch.setattr(git, "push", _noop_push)
    service = PullRequestService(
        sessions=database.sessions,
        git=git,
        secrets=InMemorySecretStore(),
        bus=EventBus(database.sessions),
        client_factory=_client_factory(fake),
    )

    async with session_scope(database.sessions) as session:
        chain = await _make_chain_for_github(session, git_settings)
        run_id = chain.run.id

    with pytest.raises(MissingSecretError):
        await service.open_pr(run_id)

    assert fake.calls == []


# -------------------------------------------------------------------- merge --
async def _merge_setup(database, git_settings: Settings):
    async with session_scope(database.sessions) as session:
        chain = await _make_chain_for_github(session, git_settings)
        await ScopedRepo(session, Worktree, chain.workspace.id).update(
            chain.worktree.id, branch="gaia/gh"
        )
        await ScopedRepo(session, PullRequest, chain.workspace.id).create(
            run_id=chain.run.id,
            number=7,
            url="https://github.com/acme/widgets/pull/7",
            state=PullRequestState.OPEN.value,
            ci_status=CiStatus.PENDING.value,
        )
        return chain


async def test_merge_finishes_when_github_already_merged(
    database,
    git_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Lead review P2: a resumed node whose PR GitHub already merged still records it,
    deletes the branch and publishes ``pr.merged`` - it does not just return."""
    fake = GitHubFake()
    fake.add_json("GET", "/repos/acme/widgets/pulls/7", _pr_payload(state="closed", merged=True))
    fake.add("DELETE", "/repos/acme/widgets/git/refs/heads/gaia/gh", httpx.Response(204))

    git = GitService(git_settings)
    monkeypatch.setattr(git, "validate_repo", _validate_repo)
    sha_calls: list[WorktreeLocation] = []

    async def fake_current_sha(location: WorktreeLocation) -> str:
        sha_calls.append(location)
        return "d" * 40

    monkeypatch.setattr(git, "current_sha", fake_current_sha)
    store = InMemorySecretStore()
    store.set(SecretName.GITHUB_TOKEN, TOKEN)
    service = PullRequestService(
        sessions=database.sessions,
        git=git,
        secrets=store,
        bus=EventBus(database.sessions),
        client_factory=_client_factory(fake),
    )
    chain = await _merge_setup(database, git_settings)

    await service.merge(chain.run.id, 7)

    assert sha_calls == []
    assert [call for call in fake.calls if call.method == "PUT"] == []
    assert any(call.method == "DELETE" for call in fake.calls)
    async with database.sessions() as session:
        rows = await ScopedRepo(session, PullRequest, chain.workspace.id).list(run_id=chain.run.id)
        assert rows[0].state == PullRequestState.MERGED.value
        events = await EventRepo(session, Event, chain.workspace.id).list_for_run(chain.run.id)
        assert [event.type for event in events] == [EventType.PR_MERGED.value]

    # A second pass finds the row already merged and makes no GitHub call at all.
    calls_before = len(fake.calls)
    await service.merge(chain.run.id, 7)
    assert len(fake.calls) == calls_before


async def test_merge_records_the_merge_even_when_the_branch_delete_fails(
    database,
    git_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Lead review P2: the merge is committed before the branch delete; a failed
    delete is logged and never undoes the recorded merge."""
    fake = GitHubFake()
    fake.add_json("GET", "/repos/acme/widgets/pulls/7", _pr_payload(state="closed", merged=True))
    fake.add_json(
        "DELETE",
        "/repos/acme/widgets/git/refs/heads/gaia/gh",
        {"message": "boom"},
        status_code=500,
    )
    git = GitService(git_settings)
    monkeypatch.setattr(git, "validate_repo", _validate_repo)
    store = InMemorySecretStore()
    store.set(SecretName.GITHUB_TOKEN, TOKEN)
    service = PullRequestService(
        sessions=database.sessions,
        git=git,
        secrets=store,
        bus=EventBus(database.sessions),
        client_factory=_client_factory(fake),
    )
    chain = await _merge_setup(database, git_settings)

    await service.merge(chain.run.id, 7)

    async with database.sessions() as session:
        rows = await ScopedRepo(session, PullRequest, chain.workspace.id).list(run_id=chain.run.id)
        assert rows[0].state == PullRequestState.MERGED.value
        events = await EventRepo(session, Event, chain.workspace.id).list_for_run(chain.run.id)
        assert [event.type for event in events] == [EventType.PR_MERGED.value]


async def test_merge_blocked_by_protection_propagates_without_retry(
    database,
    git_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = GitHubFake()
    fake.add_json("GET", "/repos/acme/widgets/pulls/7", _pr_payload())
    fake.add_json(
        "POST",
        "/graphql",
        {"data": {"markPullRequestReadyForReview": {"pullRequest": {"isDraft": False}}}},
    )
    fake.add_json(
        "PUT",
        "/repos/acme/widgets/pulls/7/merge",
        {"message": "Branch protection rules are blocking merge"},
        status_code=405,
    )

    git = GitService(git_settings)
    monkeypatch.setattr(git, "validate_repo", _validate_repo)

    async def fake_current_sha(location: WorktreeLocation) -> str:
        return "d" * 40

    monkeypatch.setattr(git, "current_sha", fake_current_sha)
    store = InMemorySecretStore()
    store.set(SecretName.GITHUB_TOKEN, TOKEN)
    service = PullRequestService(
        sessions=database.sessions,
        git=git,
        secrets=store,
        bus=EventBus(database.sessions),
        client_factory=_client_factory(fake),
    )
    chain = await _merge_setup(database, git_settings)

    with pytest.raises(MergeBlocked, match="Branch protection"):
        await service.merge(chain.run.id, 7)

    assert len([call for call in fake.calls if call.method == "PUT"]) == 1


async def test_merge_success_sends_worktree_head_deletes_branch_and_publishes(
    database,
    git_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = GitHubFake()
    fake.add_json("GET", "/repos/acme/widgets/pulls/7", _pr_payload())
    fake.add_json(
        "POST",
        "/graphql",
        {"data": {"markPullRequestReadyForReview": {"pullRequest": {"isDraft": False}}}},
    )
    fake.add_json(
        "PUT",
        "/repos/acme/widgets/pulls/7/merge",
        {"sha": "c" * 40, "merged": True},
    )
    fake.add("DELETE", "/repos/acme/widgets/git/refs/heads/gaia/gh", httpx.Response(204))

    git = GitService(git_settings)
    monkeypatch.setattr(git, "validate_repo", _validate_repo)
    seen_sha: list[str] = []

    async def fake_current_sha(location: WorktreeLocation) -> str:
        sha = "d" * 40
        seen_sha.append(sha)
        return sha

    monkeypatch.setattr(git, "current_sha", fake_current_sha)
    store = InMemorySecretStore()
    store.set(SecretName.GITHUB_TOKEN, TOKEN)
    service = PullRequestService(
        sessions=database.sessions,
        git=git,
        secrets=store,
        bus=EventBus(database.sessions),
        client_factory=_client_factory(fake),
    )
    chain = await _merge_setup(database, git_settings)

    await service.merge(chain.run.id, 7)

    assert seen_sha == ["d" * 40]
    put = next(call for call in fake.calls if call.method == "PUT")
    body = json.loads(put.content)
    assert body == {
        "merge_method": "squash",
        "sha": "d" * 40,
        "commit_title": f"[gaia] {chain.ticket.title} (#7)",
    }
    # Lead review G2: the draft is marked ready (GraphQL) before the merge.
    methods = [(call.method, call.url.path) for call in fake.calls]
    assert methods.index(("POST", "/graphql")) < methods.index(
        ("PUT", "/repos/acme/widgets/pulls/7/merge")
    )
    graphql = json.loads(next(call for call in fake.calls if call.url.path == "/graphql").content)
    assert graphql["variables"] == {"id": "PR_node_7"}
    assert any(call.method == "DELETE" for call in fake.calls)

    async with database.sessions() as session:
        rows = await ScopedRepo(session, PullRequest, chain.workspace.id).list(run_id=chain.run.id)
        assert rows[0].state == PullRequestState.MERGED.value
        events = await EventRepo(session, Event, chain.workspace.id).list_for_run(chain.run.id)
        assert [event.type for event in events] == [EventType.PR_MERGED.value]


# ------------------------------------------------------------- build_pr_body --
def _step(
    *,
    title: str,
    status: str = "completed",
    attempts: int = 1,
    result_summary: str | None = None,
) -> RunStep:
    return RunStep(
        run_id="run",
        step_index=0,
        title=title,
        spec_json={},
        status=status,
        attempts=attempts,
        result_summary=result_summary,
    )


def test_build_pr_body_renders_plan_steps_cost_and_closes() -> None:
    steps = [
        _step(
            title="Add @boss endpoint",
            status="completed",
            attempts=2,
            result_summary="@boss looked good",
        )
    ]
    body = build_pr_body(
        plan_summary="Ship @boss widgets",
        steps=steps,
        cost_usd=Decimal("12.34"),
        external_ref="github#12",
    )

    assert "## Plan" in body
    assert "Ship @\u200dboss widgets" in body
    assert "1. **Add @\u200dboss endpoint** — status: completed, attempts: 2" in body
    assert "Test results: @\u200dboss looked good" in body
    assert "## Cost" in body
    assert "$12.34" in body
    assert "Closes #12" in body
    assert "@boss" not in body


def test_build_pr_body_uses_not_recorded_for_missing_results() -> None:
    body = build_pr_body(
        plan_summary=None,
        steps=[_step(title="No tests", status="failed", attempts=1)],
        cost_usd=Decimal("1.50"),
        external_ref=None,
    )

    assert "_No plan summary recorded._" in body
    assert "Test results: not recorded" in body
    assert "Closes" not in body


def test_build_pr_body_caps_at_60_000_characters() -> None:
    body = build_pr_body(
        plan_summary="@" + ("x" * 70_000),
        steps=[],
        cost_usd=Decimal("0.00"),
        external_ref=None,
    )

    assert len(body) == 60_000


def test_build_pr_body_ignores_a_non_github_external_ref() -> None:
    body = build_pr_body(
        plan_summary="plan",
        steps=[],
        cost_usd=Decimal("0.00"),
        external_ref="jira-12",
    )

    assert "Closes" not in body


class _FakeScopedRepo:
    """A scriptable stand-in for one :class:`ScopedRepo` model."""

    def __init__(self, model: object) -> None:
        self.model = model
        self.get_result: object = None
        self.list_result: list[object] = []

    async def get(self, row_id: str) -> object:
        return self.get_result

    async def list(self, **filters: object) -> list[object]:
        return self.list_result


def _install_fake_repo_services(
    monkeypatch: pytest.MonkeyPatch,
    *,
    run: object,
    repos: dict[Any, _FakeScopedRepo],
) -> None:
    """Replace the module's DB seams with scriptable fakes for one test."""

    class FakeRunRepo:
        def __init__(self, session: AsyncSession) -> None:
            self.session = session

        async def get_by_id(self, run_id: str) -> object:
            return run

    def fake_scoped(session: AsyncSession, model: object, workspace_id: str) -> _FakeScopedRepo:
        return repos[model]

    monkeypatch.setattr(pr_module, "RunRepo", FakeRunRepo)
    monkeypatch.setattr(pr_module, "ScopedRepo", fake_scoped)


# ----------------------------------------------------------- setup failures --
async def test_open_pr_unknown_run_is_a_setup_error(
    database,
    git_settings: Settings,
) -> None:
    fake = GitHubFake()
    git = GitService(git_settings)
    service = PullRequestService(
        sessions=database.sessions,
        git=git,
        secrets=InMemorySecretStore(),
        bus=EventBus(database.sessions),
        client_factory=_client_factory(fake),
    )

    with pytest.raises(GitHubSetupError, match="no run"):
        await service.open_pr("missing-run")


async def test_open_pr_missing_ticket_is_a_setup_error(
    database,
    git_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = SimpleNamespace(
        workspace_id="ws",
        id="run",
        ticket_id="ticket",
        repo_id="repo",
        worktree_id="worktree",
        cost_usd=Decimal("0"),
    )
    repos = {Ticket: _FakeScopedRepo(Ticket)}
    _install_fake_repo_services(monkeypatch, run=run, repos=repos)
    service = PullRequestService(
        sessions=database.sessions,
        git=GitService(git_settings),
        secrets=InMemorySecretStore(),
        bus=EventBus(database.sessions),
        client_factory=_client_factory(GitHubFake()),
    )

    with pytest.raises(GitHubSetupError, match="no ticket"):
        await service.open_pr("run")


async def test_open_pr_missing_repository_is_a_setup_error(
    database,
    git_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = SimpleNamespace(
        workspace_id="ws",
        id="run",
        ticket_id="ticket",
        repo_id="repo",
        worktree_id="worktree",
        cost_usd=Decimal("0"),
    )
    repos = {
        Ticket: _FakeScopedRepo(Ticket),
        Repository: _FakeScopedRepo(Repository),
    }
    repos[Ticket].get_result = SimpleNamespace()
    _install_fake_repo_services(monkeypatch, run=run, repos=repos)
    service = PullRequestService(
        sessions=database.sessions,
        git=GitService(git_settings),
        secrets=InMemorySecretStore(),
        bus=EventBus(database.sessions),
        client_factory=_client_factory(GitHubFake()),
    )

    with pytest.raises(GitHubSetupError, match="no repository"):
        await service.open_pr("run")


async def test_open_pr_missing_worktree_row_is_a_setup_error(
    database,
    git_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = SimpleNamespace(
        workspace_id="ws",
        id="run",
        ticket_id="ticket",
        repo_id="repo",
        worktree_id="worktree",
        cost_usd=Decimal("0"),
    )
    repos = {
        Ticket: _FakeScopedRepo(Ticket),
        Repository: _FakeScopedRepo(Repository),
        Worktree: _FakeScopedRepo(Worktree),
    }
    repos[Ticket].get_result = SimpleNamespace()
    repos[Repository].get_result = SimpleNamespace()
    _install_fake_repo_services(monkeypatch, run=run, repos=repos)
    service = PullRequestService(
        sessions=database.sessions,
        git=GitService(git_settings),
        secrets=InMemorySecretStore(),
        bus=EventBus(database.sessions),
        client_factory=_client_factory(GitHubFake()),
    )

    with pytest.raises(GitHubSetupError, match="no worktree"):
        await service.open_pr("run")


async def test_open_pr_without_a_worktree_is_a_setup_error(
    database,
    git_settings: Settings,
) -> None:
    fake = GitHubFake()
    git = GitService(git_settings)
    service = PullRequestService(
        sessions=database.sessions,
        git=git,
        secrets=InMemorySecretStore(),
        bus=EventBus(database.sessions),
        client_factory=_client_factory(fake),
    )

    async with session_scope(database.sessions) as session:
        chain = await _make_chain_for_github(session, git_settings)
        await ScopedRepo(session, Run, chain.workspace.id).update(chain.run.id, worktree_id=None)
        run_id = chain.run.id

    with pytest.raises(GitHubSetupError, match="no worktree"):
        await service.open_pr(run_id)


async def test_merge_unknown_run_is_a_setup_error(
    database,
    git_settings: Settings,
) -> None:
    fake = GitHubFake()
    git = GitService(git_settings)
    service = PullRequestService(
        sessions=database.sessions,
        git=git,
        secrets=InMemorySecretStore(),
        bus=EventBus(database.sessions),
        client_factory=_client_factory(fake),
    )

    with pytest.raises(GitHubSetupError, match="no run"):
        await service.merge("missing-run", 7)


async def test_merge_without_a_worktree_is_a_setup_error(
    database,
    git_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = GitHubFake()
    git = GitService(git_settings)
    monkeypatch.setattr(git, "validate_repo", _validate_repo)
    service = PullRequestService(
        sessions=database.sessions,
        git=git,
        secrets=InMemorySecretStore(),
        bus=EventBus(database.sessions),
        client_factory=_client_factory(fake),
    )
    chain = await _merge_setup(database, git_settings)
    async with session_scope(database.sessions) as session:
        await ScopedRepo(session, Run, chain.workspace.id).update(chain.run.id, worktree_id=None)

    with pytest.raises(GitHubSetupError, match="no worktree"):
        await service.merge(chain.run.id, 7)


async def test_merge_without_a_pull_request_row_is_a_setup_error(
    database,
    git_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = GitHubFake()
    git = GitService(git_settings)
    monkeypatch.setattr(git, "validate_repo", _validate_repo)
    service = PullRequestService(
        sessions=database.sessions,
        git=git,
        secrets=InMemorySecretStore(),
        bus=EventBus(database.sessions),
        client_factory=_client_factory(fake),
    )

    async with session_scope(database.sessions) as session:
        chain = await _make_chain_for_github(session, git_settings)
        run_id = chain.run.id

    with pytest.raises(GitHubSetupError, match="no pull request"):
        await service.merge(run_id, 7)


def test_parse_repo_slug_rejects_an_unsupported_remote() -> None:
    repo = Repository(remote_url="https://gitlab.com/acme/widgets.git")
    with pytest.raises(GitHubSetupError, match="no GitHub remote"):
        _parse_repo_slug(repo)


async def test_plan_summary_ignores_a_non_dict_plan(
    database,
    git_settings: Settings,
) -> None:
    async with session_scope(database.sessions) as session:
        chain = await _make_chain_for_github(session, git_settings)
        await ScopedRepo(session, Approval, chain.workspace.id).create(
            run_id=chain.run.id,
            idempotency_key="plan:bad",
            kind=ApprovalKind.PLAN.value,
            payload_json={"plan": "not a dict"},
            status=ApprovalStatus.APPROVED.value,
        )

        assert await _plan_summary(session, chain.workspace.id, chain.run.id) is None


async def test_watch_ci_is_still_a_15b_stub(
    database,
    git_settings: Settings,
) -> None:
    fake = GitHubFake()
    git = GitService(git_settings)
    service = PullRequestService(
        sessions=database.sessions,
        git=git,
        secrets=InMemorySecretStore(),
        bus=EventBus(database.sessions),
        client_factory=_client_factory(fake),
    )

    with pytest.raises(NotImplementedError, match="TODO\\(gaia-15b\\)"):
        await service.watch_ci("run", 7)


async def test_a_failed_token_check_is_not_cached(database, git_settings: Settings) -> None:
    """Lead review P3: after the owner fixes the token, the next attempt re-checks
    instead of failing until the server restarts. A passing check is cached."""
    answers = iter([403, 200])
    fake = GitHubFake()
    fake.add_json("GET", "/repos/acme/widgets", _repo_payload())
    fake.add_json("GET", "/repos/acme/widgets/pulls", [])
    fake.add_dynamic(
        "GET",
        "/repos/acme/widgets/issues",
        lambda request: httpx.Response(next(answers), json={"message": "x"}),
    )
    fake.add_json("GET", "/repos/acme/widgets/commits/main/status", {})
    fake.add_json("GET", "/repos/acme/widgets/commits/main/check-runs", {})
    service = PullRequestService(
        sessions=database.sessions,
        git=GitService(git_settings),
        secrets=InMemorySecretStore(),
        bus=EventBus(database.sessions),
        client_factory=_client_factory(fake),
    )

    with pytest.raises(GitHubSetupError, match="Issues R"):
        await service._ensure_token_checked("repo", SecretStr(TOKEN), SLUG)
    await service._ensure_token_checked("repo", SecretStr(TOKEN), SLUG)
    calls = len(fake.calls)
    await service._ensure_token_checked("repo", SecretStr(TOKEN), SLUG)
    assert len(fake.calls) == calls
