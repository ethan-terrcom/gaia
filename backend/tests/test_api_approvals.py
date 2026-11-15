"""Approval inbox list (doc 16)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from gaia.db.models import Approval, Repository, Run, Ticket
from gaia.db.repo import ScopedRepo
from gaia.domain.enums import ApprovalKind, ApprovalStatus
from tests.conftest import Api, Database
from tests.db_builders import make_chain


async def _make_run(api: Api, database: Database, *, name: str) -> str:
    """Create a repository/ticket/run in the API's seeded workspace."""
    workspace_id = api.app.state.services.principal.workspace_id
    async with database.sessions() as session:
        repos = ScopedRepo(session, Repository, workspace_id)
        repository = await repos.create(name=name, local_path=f"/tmp/{name}")
        tickets = ScopedRepo(session, Ticket, workspace_id)
        ticket = await tickets.create(repo_id=repository.id, title=name)
        runs = ScopedRepo(session, Run, workspace_id)
        run = await runs.create(ticket_id=ticket.id, repo_id=repository.id)
        await session.commit()
    return run.id


async def _add_approval(
    api: Api,
    database: Database,
    run_id: str,
    *,
    idempotency_key: str,
    status: ApprovalStatus = ApprovalStatus.PENDING,
    created_at: datetime | None = None,
) -> None:
    workspace_id = api.app.state.services.principal.workspace_id
    values: dict[str, object] = {
        "run_id": run_id,
        "idempotency_key": idempotency_key,
        "kind": ApprovalKind.PLAN.value,
        "payload_json": {"steps": []},
        "status": status.value,
    }
    if created_at is not None:
        values["created_at"] = created_at
    async with database.sessions() as session:
        approvals = ScopedRepo(session, Approval, workspace_id)
        await approvals.create(**values)
        await session.commit()


async def test_approvals_are_oldest_first_and_pending_count_ignores_filters(
    api: Api,
    database: Database,
) -> None:
    run_id = await _make_run(api, database, name="approvals-order")
    base = datetime(2026, 1, 1, tzinfo=UTC)
    await _add_approval(
        api,
        database,
        run_id,
        idempotency_key="first",
        created_at=base,
    )
    await _add_approval(
        api,
        database,
        run_id,
        idempotency_key="second",
        created_at=base + timedelta(seconds=1),
    )
    await _add_approval(
        api,
        database,
        run_id,
        idempotency_key="decided",
        status=ApprovalStatus.APPROVED,
        created_at=base + timedelta(seconds=2),
    )

    response = await api.client.get("/api/v1/approvals", headers=api.auth)

    assert response.status_code == 200
    body = response.json()
    assert [item["idempotency_key"] for item in body["items"]] == ["first", "second", "decided"]
    assert body["total_pending"] == 2


async def test_approvals_can_filter_by_status_and_run(
    api: Api,
    database: Database,
) -> None:
    first_run = await _make_run(api, database, name="approvals-filter-a")
    second_run = await _make_run(api, database, name="approvals-filter-b")
    base = datetime(2026, 1, 2, tzinfo=UTC)
    await _add_approval(
        api,
        database,
        first_run,
        idempotency_key="a-pending",
        created_at=base,
    )
    await _add_approval(
        api,
        database,
        first_run,
        idempotency_key="a-approved",
        status=ApprovalStatus.APPROVED,
        created_at=base + timedelta(seconds=1),
    )
    await _add_approval(
        api,
        database,
        second_run,
        idempotency_key="b-pending",
        created_at=base + timedelta(seconds=2),
    )

    pending = await api.client.get(
        "/api/v1/approvals",
        params={"status": "pending"},
        headers=api.auth,
    )
    approved = await api.client.get(
        "/api/v1/approvals",
        params={"status": "approved"},
        headers=api.auth,
    )
    by_run = await api.client.get(
        "/api/v1/approvals",
        params={"run_id": first_run},
        headers=api.auth,
    )

    assert [item["idempotency_key"] for item in pending.json()["items"]] == [
        "a-pending",
        "b-pending",
    ]
    assert pending.json()["total_pending"] == 2
    assert [item["idempotency_key"] for item in approved.json()["items"]] == ["a-approved"]
    assert approved.json()["total_pending"] == 2
    assert [item["idempotency_key"] for item in by_run.json()["items"]] == [
        "a-pending",
        "a-approved",
    ]
    assert by_run.json()["total_pending"] == 2


async def test_approvals_list_hides_another_workspace(
    api: Api,
    database: Database,
    session: AsyncSession,
) -> None:
    run_id = await _make_run(api, database, name="approvals-local")
    await _add_approval(api, database, run_id, idempotency_key="local")
    foreign = await make_chain(session, slug="foreign-approvals")
    await ScopedRepo(session, Approval, foreign.workspace.id).create(
        run_id=foreign.run.id,
        idempotency_key="foreign",
        kind=ApprovalKind.PLAN,
        payload_json={},
    )
    await session.commit()

    response = await api.client.get("/api/v1/approvals", headers=api.auth)

    assert response.status_code == 200
    body = response.json()
    assert [item["idempotency_key"] for item in body["items"]] == ["local"]
    assert body["total_pending"] == 1


async def test_approvals_limit_is_enforced(api: Api, database: Database) -> None:
    run_id = await _make_run(api, database, name="approvals-limit")
    for index in range(3):
        await _add_approval(api, database, run_id, idempotency_key=f"item-{index}")

    response = await api.client.get(
        "/api/v1/approvals",
        params={"limit": 2},
        headers=api.auth,
    )

    assert response.status_code == 200
    assert len(response.json()["items"]) == 2
    assert response.json()["total_pending"] == 3
