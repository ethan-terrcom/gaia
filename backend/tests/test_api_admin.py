"""Admin kill-switch routes (doc 18a)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import replace
from decimal import Decimal
from typing import Any

import pytest
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from gaia.db.models import Approval, AuditLog, Run, Ticket
from gaia.db.repo import AuditLogRepo, ScopedRepo
from gaia.domain.enums import ApprovalKind, ApprovalStatus, RunStatus
from gaia.orchestrator.graph import build_graph
from gaia.orchestrator.pause import OrchestratorPaused, PauseSwitch
from gaia.orchestrator.runner import RunManager
from gaia.runtimes.approvals import DbApprovalBroker
from tests.conftest import Api, Database
from tests.orchestrator_helpers import make_fakes, make_graph_deps
from tests.test_api_runs import _seed_ticket_worktree

pytestmark = pytest.mark.anyio


@pytest.fixture
async def admin_api(api: Api, database: Database) -> AsyncIterator[Api]:
    """The real app with a fake-graph manager and a wired ``PauseSwitch``."""
    ticket_id = await _seed_ticket_worktree(api, database, name="admin-api")
    api.app.state.extra_test_ticket_id = ticket_id
    original = api.app.state.services
    pause = PauseSwitch(api.settings.data_dir / "paused")
    fakes = make_fakes()
    deps = make_graph_deps(database, api.settings, fakes)
    async with AsyncSqliteSaver.from_conn_string(str(api.settings.checkpoint_path)) as saver:
        await saver.setup()
        graph = build_graph(deps, checkpointer=saver)
        manager = RunManager(
            graph,
            deps,
            max_concurrent_runs=api.settings.max_concurrent_runs,
            pause=pause,
        )
        broker = DbApprovalBroker(database.sessions, deps.events)
        api.app.state.services = replace(original, runs=manager, approvals=broker, pause=pause)
        try:
            yield api
        finally:
            api.app.state.services = original
            await manager.aclose()


def _workspace_id(api: Api) -> str:
    workspace_id: str = api.app.state.services.principal.workspace_id
    return workspace_id


async def _repo_id(api: Api, database: Database, ticket_id: str) -> str:
    workspace_id = _workspace_id(api)
    async with database.sessions() as session:
        ticket = await ScopedRepo(session, Ticket, workspace_id).get(ticket_id)
        assert ticket is not None
        return ticket.repo_id


async def _add_run(
    database: Database,
    workspace_id: str,
    *,
    ticket_id: str,
    repo_id: str,
    status: RunStatus = RunStatus.PENDING,
) -> str:
    async with database.sessions() as session:
        runs = ScopedRepo(session, Run, workspace_id)
        run = await runs.create(
            ticket_id=ticket_id,
            repo_id=repo_id,
            budget_usd=Decimal("5.0"),
            status=status.value,
        )
        await session.commit()
        return run.id


async def _add_approval(
    database: Database,
    workspace_id: str,
    *,
    run_id: str,
    kind: ApprovalKind,
    idempotency_key: str,
) -> str:
    async with database.sessions() as session:
        approvals = ScopedRepo(session, Approval, workspace_id)
        approval = await approvals.create(
            run_id=run_id,
            idempotency_key=idempotency_key,
            kind=kind.value,
            payload_json={"kind": kind.value},
            status=ApprovalStatus.PENDING.value,
        )
        await session.commit()
        return approval.id


async def test_admin_routes_are_503_when_manager_is_missing(api: Api) -> None:
    methods = [
        ("GET", "/api/v1/admin/pause", None),
        ("POST", "/api/v1/admin/pause-all", {"reason": "x"}),
        ("POST", "/api/v1/admin/resume-all", None),
    ]
    for method, path, json in methods:
        response = await api.client.request(method, path, json=json, headers=api.auth)
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "orchestrator_unavailable"


async def test_pause_all_resume_all_and_audit(admin_api: Api, database: Database) -> None:
    workspace_id = _workspace_id(admin_api)
    ticket_id = admin_api.app.state.extra_test_ticket_id
    repo_id = await _repo_id(admin_api, database, ticket_id)
    run_id = await _add_run(
        database,
        workspace_id,
        ticket_id=ticket_id,
        repo_id=repo_id,
    )

    response = await admin_api.client.post(
        "/api/v1/admin/pause-all",
        json={"reason": "going out"},
        headers=admin_api.auth,
    )
    assert response.status_code == 200
    assert response.json()["paused_run_ids"] == [run_id]

    status_response = await admin_api.client.get("/api/v1/admin/pause", headers=admin_api.auth)
    assert status_response.status_code == 200
    body = status_response.json()
    assert body["paused"] is True
    assert body["reason"] == "going out"
    assert body["since"] is not None

    again = await admin_api.client.post(
        "/api/v1/admin/pause-all",
        json={"reason": "still out"},
        headers=admin_api.auth,
    )
    assert again.status_code == 200
    assert again.json()["paused_run_ids"] == []

    resume = await admin_api.client.post("/api/v1/admin/resume-all", headers=admin_api.auth)
    assert resume.status_code == 200
    assert resume.json()["resumed_run_ids"] == [run_id]

    status_response = await admin_api.client.get("/api/v1/admin/pause", headers=admin_api.auth)
    assert status_response.status_code == 200
    assert status_response.json() == {"paused": False, "since": None, "reason": None}

    async with database.sessions() as session:
        audits = AuditLogRepo(session, AuditLog, workspace_id)
        pause_rows = await audits.list(action="admin.pause_all")
        resume_rows = await audits.list(action="admin.resume_all")
        assert [row.details_json["reason"] for row in pause_rows] == ["going out", "still out"]
        assert len(resume_rows) == 1


async def test_start_route_maps_orchestrator_paused_from_the_manager(
    admin_api: Api, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = admin_api.app.state.services.runs
    assert manager is not None

    async def fake_start(*args: Any, **kwargs: Any) -> str:
        raise OrchestratorPaused()

    monkeypatch.setattr(manager, "start", fake_start)
    ticket_id = admin_api.app.state.extra_test_ticket_id

    response = await admin_api.client.post(
        "/api/v1/runs", json={"ticket_id": ticket_id}, headers=admin_api.auth
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "orchestrator_paused"


async def test_graph_gate_decision_maps_orchestrator_paused_from_the_manager(
    admin_api: Api, database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace_id = _workspace_id(admin_api)
    ticket_id = admin_api.app.state.extra_test_ticket_id
    repo_id = await _repo_id(admin_api, database, ticket_id)
    run_id = await _add_run(
        database,
        workspace_id,
        ticket_id=ticket_id,
        repo_id=repo_id,
        status=RunStatus.AWAITING_APPROVAL,
    )
    approval_id = await _add_approval(
        database,
        workspace_id,
        run_id=run_id,
        kind=ApprovalKind.PLAN,
        idempotency_key="approve_plan:0:1",
    )
    manager = admin_api.app.state.services.runs
    assert manager is not None

    async def fake_resume(run_id: str, decision: dict[str, object]) -> None:
        raise OrchestratorPaused()

    monkeypatch.setattr(manager, "resume", fake_resume)

    response = await admin_api.client.post(
        f"/api/v1/approvals/{approval_id}/decide",
        json={"decision": "approve"},
        headers=admin_api.auth,
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "orchestrator_paused"


async def test_while_paused_start_and_graph_gate_are_refused_and_command_works(
    admin_api: Api, database: Database
) -> None:
    workspace_id = _workspace_id(admin_api)
    ticket_id = admin_api.app.state.extra_test_ticket_id
    repo_id = await _repo_id(admin_api, database, ticket_id)

    paused = await admin_api.client.post(
        "/api/v1/admin/pause-all",
        json={"reason": "hold"},
        headers=admin_api.auth,
    )
    assert paused.status_code == 200

    start = await admin_api.client.post(
        "/api/v1/runs", json={"ticket_id": ticket_id}, headers=admin_api.auth
    )
    assert start.status_code == 409
    assert start.json()["error"]["code"] == "orchestrator_paused"

    gate_run_id = await _add_run(
        database,
        workspace_id,
        ticket_id=ticket_id,
        repo_id=repo_id,
        status=RunStatus.AWAITING_APPROVAL,
    )
    gate_approval_id = await _add_approval(
        database,
        workspace_id,
        run_id=gate_run_id,
        kind=ApprovalKind.PLAN,
        idempotency_key="approve_plan:0:1",
    )
    gate = await admin_api.client.post(
        f"/api/v1/approvals/{gate_approval_id}/decide",
        json={"decision": "approve"},
        headers=admin_api.auth,
    )
    assert gate.status_code == 409
    assert gate.json()["error"]["code"] == "orchestrator_paused"

    command_run_id = await _add_run(
        database,
        workspace_id,
        ticket_id=ticket_id,
        repo_id=repo_id,
        status=RunStatus.AWAITING_APPROVAL,
    )
    command_approval_id = await _add_approval(
        database,
        workspace_id,
        run_id=command_run_id,
        kind=ApprovalKind.COMMAND,
        idempotency_key="command:1",
    )
    command = await admin_api.client.post(
        f"/api/v1/approvals/{command_approval_id}/decide",
        json={"decision": "approve"},
        headers=admin_api.auth,
    )
    assert command.status_code == 200
    assert command.json()["status"] == "approved"
