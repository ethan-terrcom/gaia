"""Doc 15b ``PullRequestService.watch_ci``."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from pydantic import SecretStr

from gaia.config.secrets import InMemorySecretStore, SecretName
from gaia.config.settings import Settings
from gaia.db.engine import session_scope
from gaia.db.models import Event, PullRequest, Repository, Worktree
from gaia.db.repo import EventRepo, ScopedRepo
from gaia.domain.enums import CiStatus, EventType, PullRequestState
from gaia.services.events import EventBus
from gaia.services.git import GitService, WorktreeLocation
from gaia.services.github import GitHubClient, GitHubError, GitHubSetupError, parse_github_remote
from gaia.services.pull_requests import PullRequestService
from tests.db_builders import make_chain
from tests.github_fakes import GitHubFake, json_response

TOKEN = "github_pat_planted_0000000000000000000000000000000000"
SLUG = parse_github_remote("https://github.com/acme/widgets.git")
PUSH_URL = SLUG.https_url
SHA = "d" * 40


async def _validate_repo(path: Path | str) -> Path:
    return Path(path)


def _client_factory(fake: GitHubFake, *, max_wait_s: float = 60.0):
    def factory(token: SecretStr) -> GitHubClient:
        return GitHubClient(
            token,
            base_url="https://github.test",
            transport=httpx.MockTransport(fake),
            max_wait_s=max_wait_s,
        )

    return factory


async def _watch_setup(
    database, git_settings: Settings, *, ci_status: str = CiStatus.PENDING.value
):
    async with session_scope(database.sessions) as session:
        chain = await make_chain(session, slug="gh")
        workspace_id = chain.workspace.id
        await ScopedRepo(session, Repository, workspace_id).update(
            chain.repository.id, remote_url=PUSH_URL
        )
        worktree_path = git_settings.worktrees_root / "repo" / "wt"
        worktree_path.parent.mkdir(parents=True, exist_ok=True)
        await ScopedRepo(session, Worktree, workspace_id).update(
            chain.worktree.id,
            branch="gaia/gh",
            path=str(worktree_path),
            git_dir=str(worktree_path / "gitdir"),
        )
        await ScopedRepo(session, PullRequest, workspace_id).create(
            run_id=chain.run.id,
            number=7,
            url="https://github.com/acme/widgets/pull/7",
            state=PullRequestState.OPEN.value,
            ci_status=ci_status,
        )
        return chain


async def _watch_service(
    database,
    git_settings: Settings,
    fake: GitHubFake,
    monkeypatch: pytest.MonkeyPatch,
    *,
    ci_poll_interval_s: float = 30.0,
    ci_watch_timeout_s: float = 7200.0,
    ci_no_checks_grace_s: float = 120.0,
    max_wait_s: float = 60.0,
) -> tuple[PullRequestService, list[float]]:
    git = GitService(git_settings)
    monkeypatch.setattr(git, "validate_repo", _validate_repo)

    async def current_sha(location: WorktreeLocation) -> str:
        return SHA

    monkeypatch.setattr(git, "current_sha", current_sha)
    store = InMemorySecretStore()
    store.set(SecretName.GITHUB_TOKEN, TOKEN)
    sleeps: list[float] = []
    now = 0.0

    async def fake_sleep(value: float) -> None:
        sleeps.append(value)
        nonlocal now
        now += value

    def fake_monotonic() -> float:
        return now

    settings = Settings(
        ci_poll_interval_s=ci_poll_interval_s,
        ci_watch_timeout_s=ci_watch_timeout_s,
        ci_no_checks_grace_s=ci_no_checks_grace_s,
    )
    service = PullRequestService(
        sessions=database.sessions,
        git=git,
        secrets=store,
        bus=EventBus(database.sessions),
        client_factory=_client_factory(fake, max_wait_s=max_wait_s),
        settings=settings,
        sleep=fake_sleep,
        monotonic=fake_monotonic,
    )
    return service, sleeps


async def _events(database, workspace_id: str, run_id: str) -> list[Event]:
    async with database.sessions() as session:
        return await EventRepo(session, Event, workspace_id).list_for_run(run_id)


async def test_watch_ci_pending_then_passed_publishes_only_on_change(
    database,
    git_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = GitHubFake()
    status_calls = 0

    def status(request: httpx.Request) -> httpx.Response:
        nonlocal status_calls
        status_calls += 1
        if status_calls == 1:
            return json_response(
                200,
                {
                    "state": "pending",
                    "total_count": 1,
                    "statuses": [{"context": "ci/test", "state": "pending"}],
                },
            )
        return json_response(
            200,
            {
                "state": "success",
                "total_count": 1,
                "statuses": [{"context": "ci/test", "state": "success"}],
            },
        )

    check_calls = 0

    def checks(request: httpx.Request) -> httpx.Response:
        nonlocal check_calls
        check_calls += 1
        if check_calls == 1:
            return json_response(
                200,
                {
                    "total_count": 1,
                    "check_runs": [{"name": "ci", "status": "in_progress", "conclusion": None}],
                },
            )
        return json_response(
            200,
            {
                "total_count": 1,
                "check_runs": [{"name": "ci", "status": "completed", "conclusion": "success"}],
            },
        )

    fake.add_dynamic("GET", f"/repos/acme/widgets/commits/{SHA}/status", status)
    fake.add_dynamic("GET", f"/repos/acme/widgets/commits/{SHA}/check-runs", checks)
    service, sleeps = await _watch_service(database, git_settings, fake, monkeypatch)
    chain = await _watch_setup(database, git_settings)

    assert await service.watch_ci(chain.run.id, 7) == "passed"
    assert sleeps == [30.0]

    async with database.sessions() as session:
        rows = await ScopedRepo(session, PullRequest, chain.workspace.id).list(run_id=chain.run.id)
        assert rows[0].ci_status == CiStatus.PASSED.value

    events = await _events(database, chain.workspace.id, chain.run.id)
    assert [event.type for event in events] == [EventType.CI_STATUS.value]
    payload = events[0].payload_json
    assert payload["ci_status"] == CiStatus.PASSED.value
    assert payload["sha"] == SHA
    assert payload["checks"] == [{"name": "ci", "status": "completed", "conclusion": "success"}]

    # The worktree HEAD is watched, not whatever the PR branch points at later.
    assert all(f"/commits/{SHA}/" in call.url.path for call in fake.calls)
    assert not any("commits/a" in call.url.path for call in fake.calls)


async def test_watch_ci_failed_returns_failed(
    database,
    git_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = GitHubFake()
    fake.add_json(
        "GET",
        f"/repos/acme/widgets/commits/{SHA}/status",
        {"state": "failure", "total_count": 0, "statuses": []},
    )
    fake.add_json(
        "GET",
        f"/repos/acme/widgets/commits/{SHA}/check-runs",
        {"total_count": 0, "check_runs": []},
    )
    service, sleeps = await _watch_service(database, git_settings, fake, monkeypatch)
    chain = await _watch_setup(database, git_settings)

    assert await service.watch_ci(chain.run.id, 7) == "failed"
    assert sleeps == []

    async with database.sessions() as session:
        rows = await ScopedRepo(session, PullRequest, chain.workspace.id).list(run_id=chain.run.id)
        assert rows[0].ci_status == CiStatus.FAILED.value

    events = await _events(database, chain.workspace.id, chain.run.id)
    assert [event.type for event in events] == [EventType.CI_STATUS.value]
    assert events[0].payload_json["ci_status"] == CiStatus.FAILED.value


async def test_watch_ci_no_checks_grace_then_unknown_and_passed(
    database,
    git_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = GitHubFake()
    fake.add_json(
        "GET",
        f"/repos/acme/widgets/commits/{SHA}/status",
        {"state": "pending", "total_count": 0, "statuses": []},
    )
    fake.add_json(
        "GET",
        f"/repos/acme/widgets/commits/{SHA}/check-runs",
        {"total_count": 0, "check_runs": []},
    )
    service, sleeps = await _watch_service(
        database,
        git_settings,
        fake,
        monkeypatch,
        ci_poll_interval_s=30.0,
        ci_no_checks_grace_s=120.0,
    )
    chain = await _watch_setup(database, git_settings)

    assert await service.watch_ci(chain.run.id, 7) == "passed"
    assert sleeps == [30.0, 45.0, 67.5]

    async with database.sessions() as session:
        rows = await ScopedRepo(session, PullRequest, chain.workspace.id).list(run_id=chain.run.id)
        assert rows[0].ci_status == CiStatus.UNKNOWN.value

    events = await _events(database, chain.workspace.id, chain.run.id)
    assert [event.type for event in events] == [EventType.CI_STATUS.value]
    assert events[0].payload_json["ci_status"] == CiStatus.UNKNOWN.value


async def test_watch_ci_timeout_returns_failed_with_reason(
    database,
    git_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = GitHubFake()
    fake.add_json(
        "GET",
        f"/repos/acme/widgets/commits/{SHA}/status",
        {
            "state": "pending",
            "total_count": 1,
            "statuses": [{"context": "ci/test", "state": "pending"}],
        },
    )
    fake.add_json(
        "GET",
        f"/repos/acme/widgets/commits/{SHA}/check-runs",
        {
            "total_count": 1,
            "check_runs": [{"name": "ci", "status": "in_progress", "conclusion": None}],
        },
    )
    service, sleeps = await _watch_service(
        database,
        git_settings,
        fake,
        monkeypatch,
        ci_poll_interval_s=30.0,
        ci_watch_timeout_s=10.0,
    )
    chain = await _watch_setup(database, git_settings)

    assert await service.watch_ci(chain.run.id, 7) == "failed"
    assert sleeps == [30.0]

    events = await _events(database, chain.workspace.id, chain.run.id)
    assert [event.type for event in events] == [EventType.CI_STATUS.value]
    assert events[0].payload_json["ci_status"] == CiStatus.FAILED.value
    assert events[0].payload_json["reason"] == "timed out waiting for CI"


async def test_watch_ci_backoff_growth_and_reset(
    database,
    git_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = GitHubFake()
    status_calls = 0

    def status(request: httpx.Request) -> httpx.Response:
        nonlocal status_calls
        status_calls += 1
        if status_calls <= 3:
            return json_response(
                200,
                {
                    "state": "pending",
                    "total_count": 1,
                    "statuses": [{"context": "ci/test", "state": "pending"}],
                },
            )
        return json_response(
            200,
            {
                "state": "success",
                "total_count": 1,
                "statuses": [{"context": "ci/test", "state": "success"}],
            },
        )

    fake.add_dynamic("GET", f"/repos/acme/widgets/commits/{SHA}/status", status)
    fake.add_json(
        "GET",
        f"/repos/acme/widgets/commits/{SHA}/check-runs",
        {
            "total_count": 1,
            "check_runs": [{"name": "ci", "status": "completed", "conclusion": "success"}],
        },
    )
    service, sleeps = await _watch_service(database, git_settings, fake, monkeypatch)
    chain = await _watch_setup(database, git_settings, ci_status=CiStatus.UNKNOWN.value)

    assert await service.watch_ci(chain.run.id, 7) == "passed"
    assert sleeps == [30.0, 45.0, 67.5]

    events = await _events(database, chain.workspace.id, chain.run.id)
    assert [event.type for event in events] == [
        EventType.CI_STATUS.value,
        EventType.CI_STATUS.value,
    ]
    assert [event.payload_json["ci_status"] for event in events] == [
        CiStatus.PENDING.value,
        CiStatus.PASSED.value,
    ]


async def test_watch_ci_rate_limited_then_continues(
    database,
    git_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = GitHubFake()
    status_calls = 0

    def status(request: httpx.Request) -> httpx.Response:
        nonlocal status_calls
        status_calls += 1
        if status_calls == 1:
            return json_response(
                429,
                {"message": "rate limited"},
                headers={"retry-after": "300"},
            )
        return json_response(
            200,
            {
                "state": "success",
                "total_count": 1,
                "statuses": [{"context": "ci/test", "state": "success"}],
            },
        )

    fake.add_dynamic("GET", f"/repos/acme/widgets/commits/{SHA}/status", status)
    fake.add_json(
        "GET",
        f"/repos/acme/widgets/commits/{SHA}/check-runs",
        {
            "total_count": 1,
            "check_runs": [{"name": "ci", "status": "completed", "conclusion": "success"}],
        },
    )
    service, sleeps = await _watch_service(
        database,
        git_settings,
        fake,
        monkeypatch,
        ci_watch_timeout_s=1000.0,
        max_wait_s=0.0,
    )
    chain = await _watch_setup(database, git_settings)

    assert await service.watch_ci(chain.run.id, 7) == "passed"
    assert sleeps == [300.0]


async def test_watch_ci_other_github_error_propagates(
    database,
    git_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = GitHubFake()
    fake.add_json(
        "GET",
        f"/repos/acme/widgets/commits/{SHA}/status",
        {"message": "nope"},
        status_code=401,
    )
    service, _ = await _watch_service(database, git_settings, fake, monkeypatch)
    chain = await _watch_setup(database, git_settings)

    with pytest.raises(GitHubError):
        await service.watch_ci(chain.run.id, 7)

    async with database.sessions() as session:
        rows = await ScopedRepo(session, PullRequest, chain.workspace.id).list(run_id=chain.run.id)
        assert rows[0].ci_status == CiStatus.PENDING.value
    events = await _events(database, chain.workspace.id, chain.run.id)
    assert events == []


async def test_watch_ci_caps_event_checks_at_50(
    database,
    git_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = GitHubFake()
    fake.add_json(
        "GET",
        f"/repos/acme/widgets/commits/{SHA}/status",
        {"state": "success", "total_count": 0, "statuses": []},
    )
    fake.add_json(
        "GET",
        f"/repos/acme/widgets/commits/{SHA}/check-runs",
        {
            "total_count": 60,
            "check_runs": [
                {"name": f"check-{index}", "status": "completed", "conclusion": "success"}
                for index in range(60)
            ],
        },
    )
    service, sleeps = await _watch_service(database, git_settings, fake, monkeypatch)
    chain = await _watch_setup(database, git_settings)

    assert await service.watch_ci(chain.run.id, 7) == "passed"
    assert sleeps == []

    events = await _events(database, chain.workspace.id, chain.run.id)
    assert len(events[0].payload_json["checks"]) == 50


async def test_watch_ci_already_unknown_grace_expiry_publishes_nothing(
    database,
    git_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = GitHubFake()
    fake.add_json(
        "GET",
        f"/repos/acme/widgets/commits/{SHA}/status",
        {"state": "pending", "total_count": 0, "statuses": []},
    )
    fake.add_json(
        "GET",
        f"/repos/acme/widgets/commits/{SHA}/check-runs",
        {"total_count": 0, "check_runs": []},
    )
    service, sleeps = await _watch_service(
        database,
        git_settings,
        fake,
        monkeypatch,
        ci_no_checks_grace_s=0.0,
    )
    chain = await _watch_setup(database, git_settings, ci_status=CiStatus.UNKNOWN.value)

    assert await service.watch_ci(chain.run.id, 7) == "passed"
    assert sleeps == []

    events = await _events(database, chain.workspace.id, chain.run.id)
    assert events == []


async def test_watch_ci_no_pr_row_is_a_setup_error(
    database,
    git_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = GitHubFake()
    service, _ = await _watch_service(database, git_settings, fake, monkeypatch)
    async with session_scope(database.sessions) as session:
        chain = await make_chain(session, slug="no-pr")
        workspace_id = chain.workspace.id
        await ScopedRepo(session, Repository, workspace_id).update(
            chain.repository.id, remote_url=PUSH_URL
        )
        worktree_path = git_settings.worktrees_root / "repo" / "no-pr"
        worktree_path.parent.mkdir(parents=True, exist_ok=True)
        await ScopedRepo(session, Worktree, workspace_id).update(
            chain.worktree.id,
            path=str(worktree_path),
            git_dir=str(worktree_path / "gitdir"),
        )
        run_id = chain.run.id

    with pytest.raises(GitHubSetupError, match="no pull request"):
        await service.watch_ci(run_id, 7)
