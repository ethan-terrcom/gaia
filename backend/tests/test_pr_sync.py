"""Doc 15b ``PRSyncService``: sync_once, run_forever and the lifespan task."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest
import structlog.testing
from pydantic import SecretStr

from gaia.api import app as app_module
from gaia.api.app import create_app
from gaia.config.secrets import InMemorySecretStore, SecretName
from gaia.config.settings import Settings
from gaia.db.engine import session_scope
from gaia.db.models import Event, PullRequest, Repository
from gaia.db.repo import EventRepo, ScopedRepo
from gaia.domain.enums import CiStatus, EventType, PullRequestState
from gaia.services.events import EventBus
from gaia.services.github import GitHubClient, parse_github_remote
from gaia.services.pr_sync import PRSyncService
from tests.db_builders import make_chain
from tests.github_fakes import GitHubFake

TOKEN = "github_pat_planted_0000000000000000000000000000000000"
SLUG = parse_github_remote("https://github.com/acme/widgets.git")
PUSH_URL = SLUG.https_url
SHA = "a" * 40


def _client_factory(fake: GitHubFake, *, max_wait_s: float = 60.0):
    def factory(token: SecretStr) -> GitHubClient:
        return GitHubClient(
            token,
            base_url="https://github.test",
            transport=httpx.MockTransport(fake),
            max_wait_s=max_wait_s,
        )

    return factory


def _pr_payload(number: int = 7, *, state: str = "open", merged: bool = False) -> dict[str, object]:
    return {
        "number": number,
        "html_url": f"https://github.com/acme/widgets/pull/{number}",
        "state": state,
        "draft": True,
        "merged_at": "2026-11-14T00:00:00Z" if merged else None,
        "head": {"sha": SHA, "ref": "gaia/gh"},
        "base": {"sha": "b" * 40, "ref": "main"},
    }


async def _make_pr(
    session,
    *,
    slug: str,
    number: int = 7,
    state: str = PullRequestState.OPEN.value,
    ci_status: str = CiStatus.PENDING.value,
    review_state: str | None = None,
    head_sha: str | None = None,
    remote_url: str | None = PUSH_URL,
    pr_id: str | None = None,
):
    chain = await make_chain(session, slug=slug)
    workspace_id = chain.workspace.id
    await ScopedRepo(session, Repository, workspace_id).update(
        chain.repository.id, remote_url=remote_url
    )
    values: dict[str, object] = {
        "run_id": chain.run.id,
        "number": number,
        "url": f"https://github.com/acme/widgets/pull/{number}",
        "state": state,
        "ci_status": ci_status,
        "review_state": review_state,
        "head_sha": head_sha,
    }
    if pr_id is not None:
        values["id"] = pr_id
    pr = await ScopedRepo(session, PullRequest, workspace_id).create(**values)
    return chain, pr


def _add_github_success_routes(
    fake: GitHubFake,
    number: int,
    *,
    state: str = "open",
    merged: bool = False,
    reviews: list[dict[str, object]] | None = None,
    combined_state: str = "success",
) -> None:
    fake.add_json(
        "GET",
        f"/repos/acme/widgets/pulls/{number}",
        _pr_payload(number=number, state=state, merged=merged),
    )
    fake.add_json(
        "GET",
        f"/repos/acme/widgets/pulls/{number}/reviews",
        reviews if reviews is not None else [],
    )
    fake.add_json(
        "GET",
        f"/repos/acme/widgets/commits/{SHA}/status",
        {
            "state": combined_state,
            "total_count": 0 if combined_state == "pending" else 1,
            "statuses": []
            if combined_state == "pending"
            else [{"context": "ci/test", "state": combined_state}],
        },
    )
    fake.add_json(
        "GET",
        f"/repos/acme/widgets/commits/{SHA}/check-runs",
        {"total_count": 0, "check_runs": []},
    )


def _service(database, fake: GitHubFake, store: InMemorySecretStore, *, sleep=None):
    return PRSyncService(
        sessions=database.sessions,
        secrets=store,
        bus=EventBus(database.sessions),
        settings=Settings(pr_sync_interval_s=60),
        client_factory=_client_factory(fake),
        sleep=sleep if sleep is not None else asyncio.sleep,
    )


async def _events(database, workspace_id: str, run_id: str) -> list[Event]:
    async with database.sessions() as session:
        return await EventRepo(session, Event, workspace_id).list_for_run(run_id)


async def test_sync_once_no_token_returns_zero_without_calls(database) -> None:
    fake = GitHubFake()
    service = _service(database, fake, InMemorySecretStore())

    assert await service.sync_once() == 0
    assert fake.calls == []


async def test_sync_once_updates_row_and_publishes_pr_updated(database) -> None:
    async with session_scope(database.sessions) as session:
        chain, _ = await _make_pr(session, slug="one")

    fake = GitHubFake()
    _add_github_success_routes(
        fake,
        7,
        reviews=[
            {
                "user": {"login": "alice"},
                "state": "APPROVED",
                "submitted_at": "2026-11-14T00:00:00Z",
            }
        ],
    )
    store = InMemorySecretStore()
    store.set(SecretName.GITHUB_TOKEN, TOKEN)
    service = _service(database, fake, store)

    assert await service.sync_once() == 1

    async with database.sessions() as session:
        rows = await ScopedRepo(session, PullRequest, chain.workspace.id).list(run_id=chain.run.id)
        assert rows[0].ci_status == CiStatus.PASSED.value
        assert rows[0].review_state == "approved"
        assert rows[0].head_sha == SHA
        assert rows[0].state == PullRequestState.OPEN.value

    events = await _events(database, chain.workspace.id, chain.run.id)
    assert [event.type for event in events] == [EventType.PR_UPDATED.value]
    payload = events[0].payload_json
    assert payload == {
        "run_id": chain.run.id,
        "number": 7,
        "state": "open",
        "ci_status": "passed",
        "review_state": "approved",
    }
    assert TOKEN not in json.dumps(payload)


async def test_sync_once_no_change_publishes_nothing(database) -> None:
    async with session_scope(database.sessions) as session:
        chain, _ = await _make_pr(
            session,
            slug="one",
            ci_status=CiStatus.PASSED.value,
            review_state="approved",
            head_sha=SHA,
        )

    fake = GitHubFake()
    _add_github_success_routes(
        fake,
        7,
        reviews=[
            {
                "user": {"login": "alice"},
                "state": "APPROVED",
                "submitted_at": "2026-11-14T00:00:00Z",
            }
        ],
    )
    store = InMemorySecretStore()
    store.set(SecretName.GITHUB_TOKEN, TOKEN)
    service = _service(database, fake, store)

    assert await service.sync_once() == 0

    events = await _events(database, chain.workspace.id, chain.run.id)
    assert events == []


async def test_sync_once_one_failing_pr_does_not_stop_others(database) -> None:
    async with session_scope(database.sessions) as session:
        chain, _ = await _make_pr(session, slug="one", number=7)
        await ScopedRepo(session, PullRequest, chain.workspace.id).create(
            run_id=chain.run.id,
            number=8,
            url="https://github.com/acme/widgets/pull/8",
            state=PullRequestState.OPEN.value,
            ci_status=CiStatus.PENDING.value,
        )

    fake = GitHubFake()
    fake.add_json(
        "GET",
        "/repos/acme/widgets/pulls/7",
        {"message": "not found"},
        status_code=404,
    )
    _add_github_success_routes(fake, 8)
    store = InMemorySecretStore()
    store.set(SecretName.GITHUB_TOKEN, TOKEN)
    service = _service(database, fake, store)

    assert await service.sync_once() == 1

    events = await _events(database, chain.workspace.id, chain.run.id)
    assert len(events) == 1
    assert events[0].payload_json["number"] == 8


async def test_sync_once_picks_up_closed_and_merged_outside_gaia(database) -> None:
    async with session_scope(database.sessions) as session:
        chain, _ = await _make_pr(session, slug="one")

    fake = GitHubFake()
    _add_github_success_routes(fake, 7, state="closed", merged=True)
    store = InMemorySecretStore()
    store.set(SecretName.GITHUB_TOKEN, TOKEN)
    service = _service(database, fake, store)

    assert await service.sync_once() == 1

    async with database.sessions() as session:
        rows = await ScopedRepo(session, PullRequest, chain.workspace.id).list(run_id=chain.run.id)
        assert rows[0].state == PullRequestState.MERGED.value

    events = await _events(database, chain.workspace.id, chain.run.id)
    assert events[0].payload_json["state"] == "merged"


async def test_sync_once_rate_limited_stops_the_round_early(database) -> None:
    async with session_scope(database.sessions) as session:
        chain, _ = await _make_pr(
            session,
            slug="one",
            number=7,
            pr_id="00000000-0000-0000-0000-000000000007",
        )
        await ScopedRepo(session, PullRequest, chain.workspace.id).create(
            id="00000000-0000-0000-0000-000000000008",
            run_id=chain.run.id,
            number=8,
            url="https://github.com/acme/widgets/pull/8",
            state=PullRequestState.OPEN.value,
            ci_status=CiStatus.PENDING.value,
        )

    fake = GitHubFake()
    fake.add_json("GET", "/repos/acme/widgets/pulls/7", _pr_payload(7))
    fake.add_json("GET", "/repos/acme/widgets/pulls/7/reviews", [])
    fake.add_json(
        "GET",
        f"/repos/acme/widgets/commits/{SHA}/status",
        {"message": "rate limited"},
        status_code=429,
        headers={"retry-after": "300"},
    )
    store = InMemorySecretStore()
    store.set(SecretName.GITHUB_TOKEN, TOKEN)
    service = PRSyncService(
        sessions=database.sessions,
        secrets=store,
        bus=EventBus(database.sessions),
        settings=Settings(pr_sync_interval_s=60),
        client_factory=_client_factory(fake, max_wait_s=0.0),
    )

    assert await service.sync_once() == 0
    assert not any(call.url.path == "/repos/acme/widgets/pulls/8" for call in fake.calls)
    events = await _events(database, chain.workspace.id, chain.run.id)
    assert events == []


async def test_sync_once_skips_repos_whose_remote_does_not_parse(database) -> None:
    async with session_scope(database.sessions) as session:
        await _make_pr(session, slug="no-remote", remote_url=None)
        await _make_pr(
            session,
            slug="not-github",
            remote_url="https://example.invalid/acme/widgets.git",
        )

    fake = GitHubFake()
    store = InMemorySecretStore()
    store.set(SecretName.GITHUB_TOKEN, TOKEN)
    service = _service(database, fake, store)

    assert await service.sync_once() == 0
    assert fake.calls == []


async def test_run_forever_logs_round_failures_and_continues(
    database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = InMemorySecretStore()
    service = _service(database, GitHubFake(), store)
    sync_calls = 0

    async def fake_sync_once() -> int:
        nonlocal sync_calls
        sync_calls += 1
        if sync_calls == 1:
            raise RuntimeError("boom")
        return 0

    sleeps: list[float] = []

    async def fake_sleep(value: float) -> None:
        sleeps.append(value)
        if len(sleeps) == 2:
            raise asyncio.CancelledError

    monkeypatch.setattr(service, "sync_once", fake_sync_once)
    monkeypatch.setattr(service, "_sleep", fake_sleep)

    with pytest.raises(asyncio.CancelledError):
        await service.run_forever()

    assert sync_calls == 2
    assert sleeps == [60.0, 60.0]


async def test_run_forever_propagates_cancellation_from_sync_once(
    database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = InMemorySecretStore()
    service = _service(database, GitHubFake(), store)

    async def cancelled_sync_once() -> int:
        raise asyncio.CancelledError

    monkeypatch.setattr(service, "sync_once", cancelled_sync_once)

    with pytest.raises(asyncio.CancelledError):
        await service.run_forever()


async def test_lifespan_starts_pr_sync_when_enabled(
    database,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = InMemorySecretStore()
    monkeypatch.setattr(app_module, "get_secret_store", lambda: store)
    settings = Settings(
        data_dir=tmp_path / "gaia-data",
        db_url=database.url,
        pr_sync_enabled=True,
        pr_sync_interval_s=60,
    )
    app = create_app(settings)

    async with app.router.lifespan_context(app):
        tasks = list(app.state.services.background_tasks)
        names = [task.get_name() for task in tasks]
        assert "pr-sync" in names
        pr_sync_task = next(task for task in tasks if task.get_name() == "pr-sync")
        assert not pr_sync_task.done()

    assert pr_sync_task.cancelled()


async def test_lifespan_does_not_start_pr_sync_when_disabled(
    database,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = InMemorySecretStore()
    monkeypatch.setattr(app_module, "get_secret_store", lambda: store)
    settings = Settings(
        data_dir=tmp_path / "gaia-data",
        db_url=database.url,
        pr_sync_enabled=False,
    )
    app = create_app(settings)

    async with app.router.lifespan_context(app):
        assert list(app.state.services.background_tasks) == []


async def test_run_forever_treats_being_offline_as_a_warning(
    database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Lead review: no network is normal for a local app, so it is not an error."""
    service = _service(database, GitHubFake(), InMemorySecretStore())

    async def offline() -> int:
        raise httpx.ConnectError("no route to host")

    async def stop(value: float) -> None:
        raise asyncio.CancelledError

    monkeypatch.setattr(service, "sync_once", offline)
    monkeypatch.setattr(service, "_sleep", stop)
    with structlog.testing.capture_logs() as logs, pytest.raises(asyncio.CancelledError):
        await service.run_forever()

    assert [entry["event"] for entry in logs] == ["pr_sync.offline"]
    assert logs[0]["log_level"] == "warning"
