"""The approval decision endpoint (doc 14)."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import replace
from types import SimpleNamespace
from typing import Any, cast

import pytest
from fastapi import HTTPException, Response
from sqlalchemy.ext.asyncio import AsyncSession

import gaia.api.routes.approvals as approvals_route
from gaia.api.routes.approvals import decide_approval
from gaia.db.engine import session_scope
from gaia.db.models import Approval, PullRequest, Repository
from gaia.db.repo import ScopedRepo
from gaia.domain.enums import (
    ApprovalKind,
    ApprovalStatus,
    CiStatus,
    PullRequestState,
    RunStatus,
)
from gaia.domain.schemas import DecideRequest
from gaia.runtimes.approvals import ApprovalNotPending, DbApprovalBroker
from tests.conftest import Api, Database
from tests.db_builders import make_chain
from tests.orchestrator_fakes import make_plan
from tests.test_api_runs import (
    _make_run,
    _manager,
    _scoped_for_api,
    _seed_ticket_worktree,
)


def _direct_request() -> Any:
    """A request-like object for direct route calls that build an error envelope."""
    return cast(Any, SimpleNamespace(state=SimpleNamespace(request_id="direct")))


@pytest.fixture
async def run_api(api: Api, database: Database) -> AsyncIterator[Api]:
    """The real app with a fake-graph manager and a DB approval broker."""
    ticket_id = await _seed_ticket_worktree(api, database, name="decide-api")
    api.app.state.extra_test_ticket_id = ticket_id
    original = api.app.state.services
    async with _manager(database, api.settings) as (manager, deps):
        broker = DbApprovalBroker(database.sessions, deps.events)
        api.app.state.services = replace(original, runs=manager, approvals=broker)
        try:
            yield api
        finally:
            api.app.state.services = original


async def _repo_and_run_id(api: Api, database: Database) -> tuple[str, str]:
    workspace_id = api.app.state.services.principal.workspace_id
    async with database.sessions() as session:
        repos = ScopedRepo(session, Repository, workspace_id)
        repo = await repos.list(limit=1)
        assert repo
        repo_id = repo[0].id
    run_id = await _make_run(
        database,
        workspace_id,
        ticket_id=api.app.state.extra_test_ticket_id,
        repo_id=repo_id,
        status=RunStatus.AWAITING_APPROVAL,
    )
    return workspace_id, run_id


async def _add_approval(
    database: Database,
    workspace_id: str,
    run_id: str,
    *,
    idempotency_key: str,
    kind: ApprovalKind,
    payload: dict[str, Any],
    status: ApprovalStatus = ApprovalStatus.PENDING,
) -> str:
    async with database.sessions() as session:
        approvals = ScopedRepo(session, Approval, workspace_id)
        approval = await approvals.create(
            run_id=run_id,
            idempotency_key=idempotency_key,
            kind=kind.value,
            payload_json=payload,
            status=status.value,
        )
        await session.commit()
        return approval.id


async def test_command_decision_uses_broker_and_does_not_resume(
    run_api: Api, database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace_id, run_id = await _repo_and_run_id(run_api, database)
    approval_id = await _add_approval(
        database,
        workspace_id,
        run_id,
        idempotency_key="cmd:1",
        kind=ApprovalKind.COMMAND,
        payload={"argv": ["pytest", "-q"], "cwd": "/tmp/worktree", "reason": "tests"},
    )
    manager = run_api.app.state.services.runs
    resumed: list[Any] = []

    async def fake_resume(run_id: str, decision: dict[str, Any]) -> None:
        resumed.append((run_id, decision))

    monkeypatch.setattr(manager, "resume", fake_resume)

    response = await run_api.client.post(
        f"/api/v1/approvals/{approval_id}/decide",
        json={"decision": "approve", "comment": "go"},
        headers=run_api.auth,
    )

    assert response.status_code == 200
    assert response.json()["status"] == "approved"
    assert response.json()["decided_by"] == run_api.app.state.services.principal.user_id
    assert resumed == []
    async with database.sessions() as session:
        row = await ScopedRepo(session, Approval, workspace_id).get(approval_id)
        assert row is not None
        assert row.status == ApprovalStatus.APPROVED.value
        assert row.decided_by == run_api.app.state.services.principal.user_id


async def test_invalid_edited_plan_is_422(run_api: Api, database: Database) -> None:
    workspace_id, run_id = await _repo_and_run_id(run_api, database)
    approval_id = await _add_approval(
        database,
        workspace_id,
        run_id,
        idempotency_key="approve_plan:0:1",
        kind=ApprovalKind.PLAN,
        payload={"kind": "plan", "plan": {}},
    )

    response = await run_api.client.post(
        f"/api/v1/approvals/{approval_id}/decide",
        json={"decision": "edit", "edited_payload": {"summary": "x", "steps": []}},
        headers=run_api.auth,
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


async def test_edit_on_command_is_422(run_api: Api, database: Database) -> None:
    workspace_id, run_id = await _repo_and_run_id(run_api, database)
    approval_id = await _add_approval(
        database,
        workspace_id,
        run_id,
        idempotency_key="cmd:edit",
        kind=ApprovalKind.COMMAND,
        payload={"argv": ["true"]},
    )

    response = await run_api.client.post(
        f"/api/v1/approvals/{approval_id}/decide",
        json={"decision": "edit"},
        headers=run_api.auth,
    )

    assert response.status_code == 422


async def test_merge_without_or_with_wrong_confirm_is_422(run_api: Api, database: Database) -> None:
    workspace_id, run_id = await _repo_and_run_id(run_api, database)
    approval_id = await _add_approval(
        database,
        workspace_id,
        run_id,
        idempotency_key="approve_merge:0:1",
        kind=ApprovalKind.MERGE,
        payload={"kind": "merge"},
    )
    async with database.sessions() as session:
        prs = ScopedRepo(session, PullRequest, workspace_id)
        await prs.create(
            run_id=run_id,
            number=42,
            url="https://example.invalid/pr/42",
            state=PullRequestState.OPEN.value,
            ci_status=CiStatus.PASSED.value,
        )
        await session.commit()

    missing = await run_api.client.post(
        f"/api/v1/approvals/{approval_id}/decide",
        json={"decision": "approve"},
        headers=run_api.auth,
    )
    wrong = await run_api.client.post(
        f"/api/v1/approvals/{approval_id}/decide",
        json={"decision": "approve", "confirm": "merge 99"},
        headers=run_api.auth,
    )

    assert missing.status_code == 422
    assert missing.json()["error"]["code"] == "merge_confirmation_required"
    assert wrong.status_code == 422
    assert wrong.json()["error"]["code"] == "merge_confirmation_required"


async def test_deciding_twice_returns_409(run_api: Api, database: Database) -> None:
    workspace_id, run_id = await _repo_and_run_id(run_api, database)
    approval_id = await _add_approval(
        database,
        workspace_id,
        run_id,
        idempotency_key="approve_plan:twice:1",
        kind=ApprovalKind.PLAN,
        payload={"kind": "plan", "plan": {}},
    )

    first = await run_api.client.post(
        f"/api/v1/approvals/{approval_id}/decide",
        json={"decision": "approve"},
        headers=run_api.auth,
    )
    second = await run_api.client.post(
        f"/api/v1/approvals/{approval_id}/decide",
        json={"decision": "approve"},
        headers=run_api.auth,
    )

    assert first.status_code == 202
    assert second.status_code == 409
    assert second.json()["error"]["code"] in {"approval_not_pending", "run_busy"}


async def test_decide_in_another_workspace_is_404(
    run_api: Api, database: Database, session: AsyncSession
) -> None:
    foreign = await make_chain(session, slug="foreign-decide")
    approvals = ScopedRepo(session, Approval, foreign.workspace.id)
    foreign_approval = await approvals.create(
        run_id=foreign.run.id,
        idempotency_key="foreign",
        kind=ApprovalKind.PLAN,
        payload_json={},
    )
    await session.commit()

    response = await run_api.client.post(
        f"/api/v1/approvals/{foreign_approval.id}/decide",
        json={"decision": "approve"},
        headers=run_api.auth,
    )

    assert response.status_code == 404


async def test_decide_route_is_503_when_manager_is_missing(api: Api) -> None:
    response = await api.client.post(
        "/api/v1/approvals/missing/decide",
        json={"decision": "approve"},
        headers=api.auth,
    )

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "orchestrator_unavailable"


async def test_approvals_payloads_show_command_and_budget_details(
    run_api: Api, database: Database
) -> None:
    workspace_id, run_id = await _repo_and_run_id(run_api, database)
    await _add_approval(
        database,
        workspace_id,
        run_id,
        idempotency_key="cmd:payload",
        kind=ApprovalKind.COMMAND,
        payload={"argv": ["pytest", "-q"], "cwd": "/tmp/worktree", "reason": "run tests"},
    )
    await _add_approval(
        database,
        workspace_id,
        run_id,
        idempotency_key="budget:payload",
        kind=ApprovalKind.BUDGET,
        payload={"cost_usd": "1.50", "budget_usd": "5.0"},
    )

    response = await run_api.client.get(
        "/api/v1/approvals", params={"status": "pending"}, headers=run_api.auth
    )

    assert response.status_code == 200
    by_kind = {item["kind"]: item["payload_json"] for item in response.json()["items"]}
    assert by_kind["command"]["argv"] == ["pytest", "-q"]
    assert by_kind["command"]["cwd"] == "/tmp/worktree"
    assert by_kind["command"]["reason"] == "run tests"
    assert by_kind["budget"]["cost_usd"] == "1.50"
    assert by_kind["budget"]["budget_usd"] == "5.0"


async def test_approval_lock_reuses_an_existing_lock() -> None:
    existing = asyncio.Lock()
    approvals_route._DECIDE_LOCKS["reuse"] = existing
    try:
        assert await approvals_route._approval_lock("reuse") is existing
    finally:
        approvals_route._DECIDE_LOCKS.clear()


async def test_edited_payload_sha256_none_is_none() -> None:
    assert approvals_route._edited_payload_sha256(None) is None


async def test_merge_pr_number_reads_payload_first() -> None:
    approval = SimpleNamespace(payload_json={"pr_number": 7})
    assert (
        await approvals_route._merge_pr_number(
            cast(Any, approval),
            cast(Any, None),
        )
        == 7
    )


async def test_unknown_approval_kind_is_422(run_api: Api, database: Database) -> None:
    workspace_id, run_id = await _repo_and_run_id(run_api, database)
    async with database.sessions() as session:
        approvals = ScopedRepo(session, Approval, workspace_id)
        approval = await approvals.create(
            run_id=run_id,
            idempotency_key="unknown",
            kind="weird",
            payload_json={},
        )
        await session.commit()

    response = await run_api.client.post(
        f"/api/v1/approvals/{approval.id}/decide",
        json={"decision": "approve"},
        headers=run_api.auth,
    )

    assert response.status_code == 422


async def test_edit_on_a_graph_gate_is_422(run_api: Api, database: Database) -> None:
    workspace_id, run_id = await _repo_and_run_id(run_api, database)
    approval_id = await _add_approval(
        database,
        workspace_id,
        run_id,
        idempotency_key="approve_pr:edit:1",
        kind=ApprovalKind.PR_OPEN,
        payload={"kind": "pr_open"},
    )

    response = await run_api.client.post(
        f"/api/v1/approvals/{approval_id}/decide",
        json={"decision": "edit", "edited_payload": {}},
        headers=run_api.auth,
    )

    assert response.status_code == 422


async def test_budget_approve_passes_budget_usd_and_audits(
    run_api: Api, database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace_id, run_id = await _repo_and_run_id(run_api, database)
    approval_id = await _add_approval(
        database,
        workspace_id,
        run_id,
        idempotency_key="budget:1",
        kind=ApprovalKind.BUDGET,
        payload={"kind": "budget", "cost_usd": "1.00", "budget_usd": "5.00"},
    )
    manager = run_api.app.state.services.runs
    resumed: list[Any] = []

    async def fake_resume(run_id: str, decision: dict[str, Any]) -> None:
        resumed.append(decision)

    monkeypatch.setattr(manager, "resume", fake_resume)

    response = await run_api.client.post(
        f"/api/v1/approvals/{approval_id}/decide",
        json={"decision": "approve", "budget_usd": "12.00"},
        headers=run_api.auth,
    )

    assert response.status_code == 202
    assert resumed
    assert resumed[0]["budget_usd"] is not None


async def test_merge_uses_payload_pr_number(
    run_api: Api, database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace_id, run_id = await _repo_and_run_id(run_api, database)
    approval_id = await _add_approval(
        database,
        workspace_id,
        run_id,
        idempotency_key="approve_merge:payload:1",
        kind=ApprovalKind.MERGE,
        payload={"kind": "merge", "pr_number": 7},
    )
    manager = run_api.app.state.services.runs

    async def fake_resume(run_id: str, decision: dict[str, Any]) -> None:
        return None

    monkeypatch.setattr(manager, "resume", fake_resume)

    response = await run_api.client.post(
        f"/api/v1/approvals/{approval_id}/decide",
        json={"decision": "approve", "confirm": "merge 7"},
        headers=run_api.auth,
    )

    assert response.status_code == 202


async def test_merge_without_a_pr_row_is_422(run_api: Api, database: Database) -> None:
    workspace_id, run_id = await _repo_and_run_id(run_api, database)
    approval_id = await _add_approval(
        database,
        workspace_id,
        run_id,
        idempotency_key="approve_merge:nopr:1",
        kind=ApprovalKind.MERGE,
        payload={"kind": "merge"},
    )

    response = await run_api.client.post(
        f"/api/v1/approvals/{approval_id}/decide",
        json={"decision": "approve", "confirm": "merge 42"},
        headers=run_api.auth,
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "merge_confirmation_required"


async def test_command_decision_maps_broker_errors(
    run_api: Api, database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace_id, run_id = await _repo_and_run_id(run_api, database)
    approval_id = await _add_approval(
        database,
        workspace_id,
        run_id,
        idempotency_key="cmd:not-pending",
        kind=ApprovalKind.COMMAND,
        payload={"argv": ["true"]},
    )
    broker = run_api.app.state.services.approvals

    async def raise_not_pending(*args: Any, **kwargs: Any) -> None:
        raise ApprovalNotPending(approval_id, "approved")

    monkeypatch.setattr(broker, "decide", raise_not_pending)
    response = await run_api.client.post(
        f"/api/v1/approvals/{approval_id}/decide",
        json={"decision": "approve"},
        headers=run_api.auth,
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "approval_not_pending"

    async def raise_lookup(*args: Any, **kwargs: Any) -> None:
        raise LookupError(f"no approval {approval_id!r}")

    monkeypatch.setattr(broker, "decide", raise_lookup)
    response = await run_api.client.post(
        f"/api/v1/approvals/{approval_id}/decide",
        json={"decision": "approve"},
        headers=run_api.auth,
    )
    assert response.status_code == 404


async def test_command_decision_missing_after_broker_is_404(
    run_api: Api, database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace_id, run_id = await _repo_and_run_id(run_api, database)
    approval_id = await _add_approval(
        database,
        workspace_id,
        run_id,
        idempotency_key="cmd:deleted",
        kind=ApprovalKind.COMMAND,
        payload={"argv": ["true"]},
    )
    broker = run_api.app.state.services.approvals

    async def delete_approval(*args: Any, **kwargs: Any) -> None:
        async with session_scope(database.sessions) as session:
            await ScopedRepo(session, Approval, workspace_id).delete(approval_id)

    monkeypatch.setattr(broker, "decide", delete_approval)
    response = await run_api.client.post(
        f"/api/v1/approvals/{approval_id}/decide",
        json={"decision": "approve"},
        headers=run_api.auth,
    )

    assert response.status_code == 404


async def test_decide_function_direct_command(run_api: Api, database: Database) -> None:
    workspace_id, run_id = await _repo_and_run_id(run_api, database)
    approval_id = await _add_approval(
        database,
        workspace_id,
        run_id,
        idempotency_key="cmd:direct",
        kind=ApprovalKind.COMMAND,
        payload={"argv": ["true"]},
    )
    async with _scoped_for_api(database, run_api) as (scoped, principal, services):
        response = await decide_approval(
            approval_id,
            DecideRequest(decision="approve"),
            cast(Any, None),
            scoped,
            principal,
            services,
        )
    assert response.status_code == 200


async def test_decide_function_direct_budget(
    run_api: Api, database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    from decimal import Decimal

    workspace_id, run_id = await _repo_and_run_id(run_api, database)
    approval_id = await _add_approval(
        database,
        workspace_id,
        run_id,
        idempotency_key="budget:direct",
        kind=ApprovalKind.BUDGET,
        payload={"kind": "budget", "cost_usd": "1.00", "budget_usd": "5.00"},
    )
    manager = run_api.app.state.services.runs

    async def fake_resume(run_id: str, decision: dict[str, Any]) -> None:
        return None

    monkeypatch.setattr(manager, "resume", fake_resume)
    async with _scoped_for_api(database, run_api) as (scoped, principal, services):
        response = await decide_approval(
            approval_id,
            DecideRequest(decision="approve", budget_usd=Decimal("12.00")),
            cast(Any, None),
            scoped,
            principal,
            services,
        )
    assert response.status_code == 202


async def test_decide_function_direct_merge_payload(
    run_api: Api, database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace_id, run_id = await _repo_and_run_id(run_api, database)
    approval_id = await _add_approval(
        database,
        workspace_id,
        run_id,
        idempotency_key="approve_merge:direct:1",
        kind=ApprovalKind.MERGE,
        payload={"kind": "merge", "pr_number": 7},
    )
    manager = run_api.app.state.services.runs

    async def fake_resume(run_id: str, decision: dict[str, Any]) -> None:
        return None

    monkeypatch.setattr(manager, "resume", fake_resume)
    async with _scoped_for_api(database, run_api) as (scoped, principal, services):
        response = await decide_approval(
            approval_id,
            DecideRequest(decision="approve", confirm="merge 7"),
            cast(Any, None),
            scoped,
            principal,
            services,
        )
    assert response.status_code == 202


async def test_decide_function_direct_invalid_plan(run_api: Api, database: Database) -> None:
    workspace_id, run_id = await _repo_and_run_id(run_api, database)
    approval_id = await _add_approval(
        database,
        workspace_id,
        run_id,
        idempotency_key="approve_plan:direct:1",
        kind=ApprovalKind.PLAN,
        payload={"kind": "plan", "plan": {}},
    )
    with pytest.raises(HTTPException, match="risks"):
        async with _scoped_for_api(database, run_api) as (scoped, principal, services):
            await decide_approval(
                approval_id,
                DecideRequest(decision="edit", edited_payload={"summary": "x", "steps": []}),
                cast(Any, None),
                scoped,
                principal,
                services,
            )


async def test_decide_cleanup_keeps_a_replaced_lock(
    run_api: Api, database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace_id, run_id = await _repo_and_run_id(run_api, database)
    approval_id = await _add_approval(
        database,
        workspace_id,
        run_id,
        idempotency_key="cmd:cleanup-direct",
        kind=ApprovalKind.COMMAND,
        payload={"argv": ["true"]},
    )
    replacement = asyncio.Lock()

    async def fake_decide(*args: Any, **kwargs: Any) -> Response:
        approvals_route._DECIDE_LOCKS[approval_id] = replacement
        return Response(status_code=204)

    monkeypatch.setattr(approvals_route, "_decide_locked", fake_decide)
    try:
        async with _scoped_for_api(database, run_api) as (scoped, principal, services):
            await decide_approval(
                approval_id,
                DecideRequest(decision="approve"),
                _direct_request(),
                scoped,
                principal,
                services,
            )
        assert approvals_route._DECIDE_LOCKS.get(approval_id) is replacement
    finally:
        approvals_route._DECIDE_LOCKS.clear()


async def test_decide_function_direct_broker_not_pending(
    run_api: Api, database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace_id, run_id = await _repo_and_run_id(run_api, database)
    approval_id = await _add_approval(
        database,
        workspace_id,
        run_id,
        idempotency_key="cmd:broker-not-pending-direct",
        kind=ApprovalKind.COMMAND,
        payload={"argv": ["true"]},
    )
    broker = run_api.app.state.services.approvals

    async def raise_not_pending(*args: Any, **kwargs: Any) -> None:
        raise ApprovalNotPending(approval_id, "approved")

    monkeypatch.setattr(broker, "decide", raise_not_pending)
    async with _scoped_for_api(database, run_api) as (scoped, principal, services):
        response = await decide_approval(
            approval_id,
            DecideRequest(decision="approve"),
            _direct_request(),
            scoped,
            principal,
            services,
        )
    assert response.status_code == 409


async def test_decide_function_direct_broker_lookup_error(
    run_api: Api, database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace_id, run_id = await _repo_and_run_id(run_api, database)
    approval_id = await _add_approval(
        database,
        workspace_id,
        run_id,
        idempotency_key="cmd:broker-lookup-direct",
        kind=ApprovalKind.COMMAND,
        payload={"argv": ["true"]},
    )
    broker = run_api.app.state.services.approvals

    async def raise_lookup(*args: Any, **kwargs: Any) -> None:
        raise LookupError(f"no approval {approval_id!r}")

    monkeypatch.setattr(broker, "decide", raise_lookup)
    async with _scoped_for_api(database, run_api) as (scoped, principal, services):
        with pytest.raises(HTTPException, match="approval not found"):
            await decide_approval(
                approval_id,
                DecideRequest(decision="approve"),
                _direct_request(),
                scoped,
                principal,
                services,
            )


async def test_decide_function_direct_missing_approval(run_api: Api, database: Database) -> None:
    async with _scoped_for_api(database, run_api) as (scoped, principal, services):
        with pytest.raises(HTTPException, match="approval not found"):
            await decide_approval(
                "missing-approval",
                DecideRequest(decision="approve"),
                cast(Any, None),
                scoped,
                principal,
                services,
            )


async def test_decide_function_direct_not_pending(run_api: Api, database: Database) -> None:
    workspace_id, run_id = await _repo_and_run_id(run_api, database)
    approval_id = await _add_approval(
        database,
        workspace_id,
        run_id,
        idempotency_key="cmd:not-pending-direct",
        kind=ApprovalKind.COMMAND,
        payload={"argv": ["true"]},
        status=ApprovalStatus.APPROVED,
    )
    async with _scoped_for_api(database, run_api) as (scoped, principal, services):
        response = await decide_approval(
            approval_id,
            DecideRequest(decision="approve"),
            _direct_request(),
            scoped,
            principal,
            services,
        )
    assert response.status_code == 409


async def test_decide_function_direct_unknown_kind(run_api: Api, database: Database) -> None:
    workspace_id, run_id = await _repo_and_run_id(run_api, database)
    async with database.sessions() as session:
        approvals = ScopedRepo(session, Approval, workspace_id)
        approval = await approvals.create(
            run_id=run_id,
            idempotency_key="unknown-direct",
            kind="weird",
            payload_json={},
        )
        await session.commit()
    async with _scoped_for_api(database, run_api) as (scoped, principal, services):
        with pytest.raises(HTTPException, match="unknown approval kind"):
            await decide_approval(
                approval.id,
                DecideRequest(decision="approve"),
                cast(Any, None),
                scoped,
                principal,
                services,
            )


async def test_decide_function_direct_command_edit(run_api: Api, database: Database) -> None:
    workspace_id, run_id = await _repo_and_run_id(run_api, database)
    approval_id = await _add_approval(
        database,
        workspace_id,
        run_id,
        idempotency_key="cmd:edit-direct",
        kind=ApprovalKind.COMMAND,
        payload={"argv": ["true"]},
    )
    async with _scoped_for_api(database, run_api) as (scoped, principal, services):
        with pytest.raises(HTTPException, match="edit is not valid"):
            await decide_approval(
                approval_id,
                DecideRequest(decision="edit"),
                cast(Any, None),
                scoped,
                principal,
                services,
            )


async def test_decide_function_direct_edit_on_non_plan(run_api: Api, database: Database) -> None:
    workspace_id, run_id = await _repo_and_run_id(run_api, database)
    approval_id = await _add_approval(
        database,
        workspace_id,
        run_id,
        idempotency_key="approve_pr:edit-direct:1",
        kind=ApprovalKind.PR_OPEN,
        payload={"kind": "pr_open"},
    )
    async with _scoped_for_api(database, run_api) as (scoped, principal, services):
        with pytest.raises(HTTPException, match="edit is only valid"):
            await decide_approval(
                approval_id,
                DecideRequest(decision="edit", edited_payload={}),
                cast(Any, None),
                scoped,
                principal,
                services,
            )


async def test_decide_function_direct_valid_plan_edit(
    run_api: Api, database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace_id, run_id = await _repo_and_run_id(run_api, database)
    approval_id = await _add_approval(
        database,
        workspace_id,
        run_id,
        idempotency_key="approve_plan:edit-direct:1",
        kind=ApprovalKind.PLAN,
        payload={"kind": "plan", "plan": {}},
    )
    manager = run_api.app.state.services.runs

    async def fake_resume(run_id: str, decision: dict[str, Any]) -> None:
        return None

    monkeypatch.setattr(manager, "resume", fake_resume)
    async with _scoped_for_api(database, run_api) as (scoped, principal, services):
        response = await decide_approval(
            approval_id,
            DecideRequest(decision="edit", edited_payload=make_plan(1).model_dump(mode="json")),
            cast(Any, None),
            scoped,
            principal,
            services,
        )
    assert response.status_code == 202


async def test_decide_function_direct_run_busy(run_api: Api, database: Database) -> None:
    workspace_id, run_id = await _repo_and_run_id(run_api, database)
    approval_id = await _add_approval(
        database,
        workspace_id,
        run_id,
        idempotency_key="approve_plan:busy:1",
        kind=ApprovalKind.PLAN,
        payload={"kind": "plan", "plan": {}},
    )
    manager = run_api.app.state.services.runs
    task = asyncio.create_task(asyncio.Event().wait())
    manager._tasks[run_id] = task
    try:
        async with _scoped_for_api(database, run_api) as (scoped, principal, services):
            response = await decide_approval(
                approval_id,
                DecideRequest(decision="approve"),
                _direct_request(),
                scoped,
                principal,
                services,
            )
        assert response.status_code == 409
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_merge_pr_number_falls_back_to_latest_pr(run_api: Api, database: Database) -> None:
    workspace_id, run_id = await _repo_and_run_id(run_api, database)
    approval_id = await _add_approval(
        database,
        workspace_id,
        run_id,
        idempotency_key="approve_merge:fallback:1",
        kind=ApprovalKind.MERGE,
        payload={"kind": "merge"},
    )
    async with database.sessions() as session:
        prs = ScopedRepo(session, PullRequest, workspace_id)
        await prs.create(
            run_id=run_id,
            number=42,
            url="https://example.invalid/pr/42",
            state=PullRequestState.OPEN.value,
            ci_status=CiStatus.PASSED.value,
        )
        await session.commit()
    async with _scoped_for_api(database, run_api) as (scoped, _principal, _services):
        approval = await scoped.repo(Approval).get(approval_id)
        assert approval is not None
        assert await approvals_route._merge_pr_number(approval, scoped) == 42


async def test_decide_function_direct_merge_no_pr(run_api: Api, database: Database) -> None:
    workspace_id, run_id = await _repo_and_run_id(run_api, database)
    approval_id = await _add_approval(
        database,
        workspace_id,
        run_id,
        idempotency_key="approve_merge:direct-nopr:1",
        kind=ApprovalKind.MERGE,
        payload={"kind": "merge"},
    )
    async with _scoped_for_api(database, run_api) as (scoped, principal, services):
        response = await decide_approval(
            approval_id,
            DecideRequest(decision="approve", confirm="merge 42"),
            _direct_request(),
            scoped,
            principal,
            services,
        )
    assert response.status_code == 422


async def test_decide_function_direct_merge_wrong_confirm(run_api: Api, database: Database) -> None:
    workspace_id, run_id = await _repo_and_run_id(run_api, database)
    approval_id = await _add_approval(
        database,
        workspace_id,
        run_id,
        idempotency_key="approve_merge:direct-wrong:1",
        kind=ApprovalKind.MERGE,
        payload={"kind": "merge"},
    )
    async with database.sessions() as session:
        prs = ScopedRepo(session, PullRequest, workspace_id)
        await prs.create(
            run_id=run_id,
            number=42,
            url="https://example.invalid/pr/42",
            state=PullRequestState.OPEN.value,
            ci_status=CiStatus.PASSED.value,
        )
        await session.commit()
    async with _scoped_for_api(database, run_api) as (scoped, principal, services):
        response = await decide_approval(
            approval_id,
            DecideRequest(decision="approve", confirm="merge 99"),
            _direct_request(),
            scoped,
            principal,
            services,
        )
    assert response.status_code == 422
