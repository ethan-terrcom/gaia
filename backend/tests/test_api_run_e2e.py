"""A full fake run driven only through the HTTP API (doc 14 acceptance)."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import replace
from typing import Any, cast

import pytest
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from gaia.db.engine import session_scope
from gaia.db.models import PullRequest
from gaia.db.repo import ScopedRepo
from gaia.domain.enums import CiStatus, PullRequestState
from gaia.orchestrator.graph import build_graph
from gaia.orchestrator.runner import RunManager
from gaia.runtimes.approvals import DbApprovalBroker
from tests.conftest import Api, Database
from tests.orchestrator_helpers import make_fakes, make_graph_deps
from tests.test_api_runs import _seed_ticket_worktree


class DbFakePullRequests:
    """A 12a fake that records the PR row, so the merge gate can read it."""

    def __init__(self, database: Database, workspace_id: str, *, pr_number: int = 42) -> None:
        self.database = database
        self.workspace_id = workspace_id
        self.pr_number = pr_number
        self.open_calls: list[str] = []
        self.watch_calls: list[tuple[str, int]] = []
        self.merge_calls: list[tuple[str, int]] = []

    async def open_pr(self, run_id: str) -> int:
        self.open_calls.append(run_id)
        async with session_scope(self.database.sessions) as session:
            prs = ScopedRepo(session, PullRequest, self.workspace_id)
            await prs.create(
                run_id=run_id,
                number=self.pr_number,
                url=f"https://example.invalid/pr/{self.pr_number}",
                state=PullRequestState.OPEN.value,
                ci_status=CiStatus.PASSED.value,
            )
        return self.pr_number

    async def watch_ci(self, run_id: str, pr: int) -> str:
        self.watch_calls.append((run_id, pr))
        return "passed"

    async def merge(self, run_id: str, pr: int) -> None:
        self.merge_calls.append((run_id, pr))


@asynccontextmanager
async def _manager(
    database: Database, settings: Any, workspace_id: str
) -> AsyncIterator[RunManager]:
    fakes = make_fakes(pull_requests=cast(Any, DbFakePullRequests(database, workspace_id)))
    deps = make_graph_deps(database, settings, fakes)
    async with AsyncSqliteSaver.from_conn_string(str(settings.checkpoint_path)) as saver:
        await saver.setup()
        graph = build_graph(deps, checkpointer=saver)
        manager = RunManager(graph, deps, max_concurrent_runs=settings.max_concurrent_runs)
        try:
            yield manager
        finally:
            await manager.aclose()


@pytest.fixture
async def run_api(api: Api, database: Database) -> AsyncIterator[Api]:
    ticket_id = await _seed_ticket_worktree(api, database, name="e2e")
    api.app.state.extra_test_ticket_id = ticket_id
    original = api.app.state.services
    workspace_id = original.principal.workspace_id
    async with _manager(database, api.settings, workspace_id) as manager:
        broker = DbApprovalBroker(database.sessions, manager.deps.events)
        api.app.state.services = replace(original, runs=manager, approvals=broker)
        try:
            yield api
        finally:
            api.app.state.services = original


async def _wait_for_approval(api: Api, kind: str, *, timeout_s: float = 10.0) -> dict[str, Any]:
    deadline = asyncio.get_running_loop().time() + timeout_s
    while asyncio.get_running_loop().time() < deadline:
        response = await api.client.get(
            "/api/v1/approvals", params={"status": "pending"}, headers=api.auth
        )
        assert response.status_code == 200
        body = cast(dict[str, Any], response.json())
        for item in body["items"]:
            if item["kind"] == kind:
                return cast(dict[str, Any], item)
        await asyncio.sleep(0.05)
    raise AssertionError(f"no pending {kind!r} approval appeared")


async def _wait_for_run_status(
    api: Api, run_id: str, status: str, *, timeout_s: float = 10.0
) -> dict[str, Any]:
    deadline = asyncio.get_running_loop().time() + timeout_s
    last: dict[str, Any] = {}
    while asyncio.get_running_loop().time() < deadline:
        response = await api.client.get(f"/api/v1/runs/{run_id}", headers=api.auth)
        assert response.status_code == 200
        last = cast(dict[str, Any], response.json())
        if last["run"]["status"] == status:
            return last
        await asyncio.sleep(0.05)
    raise AssertionError(f"run {run_id} never reached {status!r}: {last}")


async def test_full_run_can_be_driven_through_the_api(run_api: Api) -> None:
    ticket_id = run_api.app.state.extra_test_ticket_id
    started = await run_api.client.post(
        "/api/v1/runs",
        json={"ticket_id": ticket_id},
        headers=run_api.auth,
    )
    assert started.status_code == 201
    run_id = started.json()["id"]

    plan = await _wait_for_approval(run_api, "plan")
    decided_plan = await run_api.client.post(
        f"/api/v1/approvals/{plan['id']}/decide",
        json={"decision": "approve"},
        headers=run_api.auth,
    )
    assert decided_plan.status_code == 202
    assert decided_plan.json()["status"] == "accepted"

    pr = await _wait_for_approval(run_api, "pr_open")
    decided_pr = await run_api.client.post(
        f"/api/v1/approvals/{pr['id']}/decide",
        json={"decision": "approve"},
        headers=run_api.auth,
    )
    assert decided_pr.status_code == 202

    merge = await _wait_for_approval(run_api, "merge")
    decided_merge = await run_api.client.post(
        f"/api/v1/approvals/{merge['id']}/decide",
        json={"decision": "approve", "confirm": "merge 42"},
        headers=run_api.auth,
    )
    assert decided_merge.status_code == 202

    detail = await _wait_for_run_status(run_api, run_id, "completed")
    assert detail["run"]["status"] == "completed"
