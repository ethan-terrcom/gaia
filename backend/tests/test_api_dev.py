"""Tests for the dev-only fake-run route and its feature flag (doc 13)."""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import pytest
from fastapi import HTTPException

from gaia.api.app import create_app
from gaia.api.routes.dev import _find_or_create_run, _publish_fake_run
from gaia.config.settings import Settings
from gaia.db.models import Event, Repository, Run, Ticket
from gaia.db.repo import EventRepo, ScopedRepo
from gaia.services.events import EventBus
from tests.conftest import API_BASE_URL, Api, Database


async def test_dev_route_is_absent_when_dev_routes_is_false(api: Api) -> None:
    response = await api.client.post("/api/v1/dev/fake-run", json={}, headers=api.auth)

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


async def test_dev_fake_run_publishes_a_scripted_sequence(
    api: Api,
    database: Database,
    tmp_path: Path,
) -> None:
    app = create_app(Settings(data_dir=tmp_path / "dev-data", db_url=database.url, dev_routes=True))
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url=API_BASE_URL) as client:
            response = await client.post(
                "/api/v1/dev/fake-run",
                json={"events": 1, "interval_s": 0.01},
                headers=api.auth,
            )

            assert response.status_code == 200
            run_id = response.json()["run_id"]

            # The route returns before the scripted events have all been
            # published; give the background task a moment and then inspect the
            # append-only event table.
            workspace_id = api.app.state.services.principal.workspace_id
            for _ in range(50):
                async with database.sessions() as session:
                    events = EventRepo(session, Event, workspace_id)
                    rows = await events.list_for_run(run_id)
                if len(rows) >= 5:
                    break
                await asyncio.sleep(0.01)

            assert [row.type for row in rows] == [
                "run.status_changed",
                "agent.message",
                "agent.tool_call",
                "agent.tool_result",
                "step.finished",
            ]


async def test_find_or_create_run_finds_an_existing_run(
    api: Api,
    database: Database,
) -> None:
    workspace_id = api.app.state.services.principal.workspace_id
    async with database.sessions() as session:
        repository = await ScopedRepo(session, Repository, workspace_id).create(
            name="dev-existing-repo",
            local_path="/tmp/dev-existing-repo",
        )
        ticket = await ScopedRepo(session, Ticket, workspace_id).create(
            repo_id=repository.id,
            title="Dev existing",
        )
        run = await ScopedRepo(session, Run, workspace_id).create(
            ticket_id=ticket.id,
            repo_id=repository.id,
        )
        await session.commit()

    found = await _find_or_create_run(
        api.app.state.services,
        api.app.state.services.principal,
        run.id,
    )

    assert found.id == run.id


async def test_find_or_create_run_rejects_a_missing_run(
    api: Api,
) -> None:
    with pytest.raises(HTTPException) as excinfo:
        await _find_or_create_run(
            api.app.state.services,
            api.app.state.services.principal,
            "missing-run",
        )

    assert excinfo.value.status_code == 404


async def test_find_or_create_run_creates_a_scratch_run(
    api: Api,
) -> None:
    run = await _find_or_create_run(
        api.app.state.services,
        api.app.state.services.principal,
        None,
    )

    assert run.id
    assert run.workspace_id == api.app.state.services.principal.workspace_id


async def test_publish_fake_run_reraises_cancellation(
    database: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bus = EventBus(database.sessions)

    async def cancelled_publish(**kwargs: object) -> None:
        raise asyncio.CancelledError

    monkeypatch.setattr(bus, "publish", cancelled_publish)

    with pytest.raises(asyncio.CancelledError):
        await _publish_fake_run(
            bus,
            workspace_id="ws-1",
            run_id="run-1",
            events=1,
            interval_s=0,
        )


async def test_publish_fake_run_logs_and_reraises_other_errors(
    database: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bus = EventBus(database.sessions)

    async def broken_publish(**kwargs: object) -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr(bus, "publish", broken_publish)

    with pytest.raises(RuntimeError, match="boom"):
        await _publish_fake_run(
            bus,
            workspace_id="ws-1",
            run_id="run-1",
            events=1,
            interval_s=0,
        )
