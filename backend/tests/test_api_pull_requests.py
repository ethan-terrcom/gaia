"""Doc 15b ``GET /api/v1/pull-requests``."""

from __future__ import annotations

from datetime import UTC, datetime

from gaia.api.deps import Scoped
from gaia.api.routes.pull_requests import list_pull_requests
from gaia.db.models import PullRequest, Repository, Run, Ticket
from gaia.db.repo import ScopedRepo
from gaia.domain.enums import CiStatus, PullRequestState, RunStatus
from tests.conftest import Api, Database
from tests.db_builders import make_workspace


def _dt(hour: int) -> datetime:
    return datetime(2026, 11, 14, hour, 0, 0, tzinfo=UTC)


async def _create_pr(
    database: Database,
    workspace_id: str,
    *,
    number: int,
    state: str = PullRequestState.OPEN.value,
    created_at: datetime,
    title: str | None = None,
) -> tuple[PullRequest, Run, Ticket, Repository]:
    async with database.sessions() as session:
        repositories = ScopedRepo(session, Repository, workspace_id)
        repository = await repositories.create(
            name=f"repo-{number}",
            local_path=f"/tmp/repo-{number}",
            remote_url=f"https://github.com/acme/repo-{number}.git",
        )
        ticket = await ScopedRepo(session, Ticket, workspace_id).create(
            repo_id=repository.id,
            title=title or f"Ticket {number}",
        )
        run = await ScopedRepo(session, Run, workspace_id).create(
            ticket_id=ticket.id,
            repo_id=repository.id,
            status=RunStatus.PR_OPEN.value,
        )
        pr = await ScopedRepo(session, PullRequest, workspace_id).create(
            run_id=run.id,
            number=number,
            url=f"https://github.com/acme/repo-{number}/pull/{number}",
            state=state,
            ci_status=CiStatus.PENDING.value,
            review_state="approved",
            head_sha="a" * 40,
            created_at=created_at,
        )
        await session.commit()
    return pr, run, ticket, repository


async def test_list_pull_requests_is_scoped_filtered_and_ordered(
    api: Api, database: Database
) -> None:
    workspace_id = api.app.state.services.principal.workspace_id
    await _create_pr(database, workspace_id, number=10, created_at=_dt(10))
    await _create_pr(
        database,
        workspace_id,
        number=11,
        state=PullRequestState.CLOSED.value,
        created_at=_dt(11),
    )
    await _create_pr(database, workspace_id, number=12, created_at=_dt(12))

    async with database.sessions() as session:
        other = await make_workspace(session, slug="other")
        other_workspace_id = other.id
        await session.commit()
    await _create_pr(database, other_workspace_id, number=99, created_at=_dt(13))

    response = await api.client.get("/api/v1/pull-requests", headers=api.auth)
    assert response.status_code == 200
    body = response.json()
    assert [item["number"] for item in body["items"]] == [12, 11, 10]
    assert all(item["workspace_id"] if "workspace_id" in item else True for item in body["items"])
    assert "workspace_id" not in body["items"][0]

    item = body["items"][0]
    assert item["number"] == 12
    assert item["state"] == "open"
    assert item["ci_status"] == "pending"
    assert item["review_state"] == "approved"
    assert item["head_sha"] == "a" * 40
    assert item["run_status"] == RunStatus.PR_OPEN.value
    assert item["ticket_title"] == "Ticket 12"
    assert item["repo_name"] == "repo-12"
    assert item["created_at"].startswith("2026-11-14T12:00:00")
    assert item["updated_at"]

    open_response = await api.client.get(
        "/api/v1/pull-requests", params={"state": "open"}, headers=api.auth
    )
    assert [item["number"] for item in open_response.json()["items"]] == [12, 10]

    closed_response = await api.client.get(
        "/api/v1/pull-requests", params={"state": "closed"}, headers=api.auth
    )
    assert [item["number"] for item in closed_response.json()["items"]] == [11]


async def test_list_pull_requests_limit_is_honoured(api: Api, database: Database) -> None:
    workspace_id = api.app.state.services.principal.workspace_id
    await _create_pr(database, workspace_id, number=10, created_at=_dt(10))
    await _create_pr(database, workspace_id, number=12, created_at=_dt(12))

    response = await api.client.get("/api/v1/pull-requests", params={"limit": 1}, headers=api.auth)
    assert [item["number"] for item in response.json()["items"]] == [12]


async def test_list_pull_requests_requires_a_token(api: Api) -> None:
    response = await api.client.get("/api/v1/pull-requests")

    assert response.status_code == 401


async def test_list_pull_requests_direct_call(api: Api, database: Database) -> None:
    workspace_id = api.app.state.services.principal.workspace_id
    await _create_pr(database, workspace_id, number=20, created_at=_dt(14))

    async with database.sessions() as session:
        page = await list_pull_requests(Scoped(session, workspace_id))

    assert [item.number for item in page.items] == [20]


async def test_get_pull_request_returns_desk_fields(api: Api, database: Database) -> None:
    from gaia.db.models import Approval
    from gaia.domain.enums import ApprovalKind, ApprovalStatus

    workspace_id = api.app.state.services.principal.workspace_id
    pr, run, _ticket, _repo = await _create_pr(
        database, workspace_id, number=30, created_at=_dt(15)
    )

    async with database.sessions() as session:
        await ScopedRepo(session, Approval, workspace_id).create(
            run_id=run.id,
            kind=ApprovalKind.MERGE.value,
            status=ApprovalStatus.PENDING.value,
            idempotency_key=f"merge-{run.id}",
            payload_json={"pr_number": 30},
        )
        await session.commit()

    response = await api.client.get(f"/api/v1/pull-requests/{pr.id}", headers=api.auth)
    assert response.status_code == 200
    body = response.json()
    assert body["id"] == pr.id
    assert body["number"] == 30
    assert body["run_id"] == run.id
    assert body["ticket_title"] == "Ticket 30"
    assert body["pending_approvals"] == [
        {"id": body["pending_approvals"][0]["id"], "kind": "merge"}
    ]
    assert body["pending_approvals"][0]["id"]


async def test_get_pull_request_is_404_for_foreign_workspace(api: Api, database: Database) -> None:
    async with database.sessions() as session:
        other = await make_workspace(session, slug="other-pr-detail")
        other_workspace_id = other.id
        await session.commit()
    pr, _run, _ticket, _repo = await _create_pr(
        database, other_workspace_id, number=40, created_at=_dt(16)
    )

    response = await api.client.get(f"/api/v1/pull-requests/{pr.id}", headers=api.auth)
    assert response.status_code == 404
