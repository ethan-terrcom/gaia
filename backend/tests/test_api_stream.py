"""HTTP tests for stream tickets, SSE and run event history (doc 13)."""

from __future__ import annotations

import pytest
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from gaia.api.deps import Scoped
from gaia.api.routes import stream as stream_module
from gaia.api.routes.stream import list_run_events
from gaia.db.models import Repository, Run, Ticket
from gaia.db.repo import ScopedRepo
from gaia.domain.enums import EventType
from tests.conftest import Api, Database
from tests.db_builders import make_chain


async def _run_in_workspace(api: Api, database: Database, *, name: str) -> str:
    """Create a repository/ticket/run in the API's seeded workspace."""
    workspace_id = api.app.state.services.principal.workspace_id
    async with database.sessions() as session:
        repos = ScopedRepo(session, Repository, workspace_id)
        repository = await repos.create(
            name=name,
            local_path=f"/tmp/{name}",
        )
        tickets = ScopedRepo(session, Ticket, workspace_id)
        ticket = await tickets.create(repo_id=repository.id, title=name)
        runs = ScopedRepo(session, Run, workspace_id)
        run = await runs.create(ticket_id=ticket.id, repo_id=repository.id)
        await session.commit()
    return run.id


async def test_create_stream_ticket_returns_a_ticket_and_ttl(api: Api) -> None:
    response = await api.client.post("/api/v1/stream/ticket", headers=api.auth)

    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"ticket", "expires_in"}
    assert len(body["ticket"]) == 43
    assert body["expires_in"] == 60.0


async def test_stream_requires_a_ticket(api: Api) -> None:
    response = await api.client.get("/api/v1/stream")

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthorized"


async def test_stream_does_not_accept_the_bearer_token_alone(api: Api) -> None:
    response = await api.client.get("/api/v1/stream", headers=api.auth)

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthorized"


async def test_stream_uses_a_ticket_once(api: Api, monkeypatch: pytest.MonkeyPatch) -> None:
    ticket = (await api.client.post("/api/v1/stream/ticket", headers=api.auth)).json()["ticket"]
    monkeypatch.setattr(stream_module, "SSE_MAX_EVENTS", 0)

    first = await api.client.get("/api/v1/stream", params={"ticket": ticket})
    second = await api.client.get("/api/v1/stream", params={"ticket": ticket})

    assert first.status_code == 200
    assert first.text == ""
    assert second.status_code == 401


async def test_stream_run_id_is_scoped_to_the_ticket_workspace(
    api: Api,
    database: Database,
) -> None:
    ticket = (await api.client.post("/api/v1/stream/ticket", headers=api.auth)).json()["ticket"]

    response = await api.client.get(
        "/api/v1/stream",
        params={"ticket": ticket, "run_id": "missing-run"},
    )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


async def test_stream_accepts_a_run_in_the_ticket_workspace(
    api: Api,
    database: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id = await _run_in_workspace(api, database, name="valid-run-stream")
    ticket = (await api.client.post("/api/v1/stream/ticket", headers=api.auth)).json()["ticket"]
    monkeypatch.setattr(stream_module, "SSE_MAX_EVENTS", 0)

    response = await api.client.get(
        "/api/v1/stream",
        params={"ticket": ticket, "run_id": run_id},
    )

    assert response.status_code == 200
    assert response.text == ""


async def test_last_event_id_header_wins_over_the_query_form(
    api: Api,
    database: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id = await _run_in_workspace(api, database, name="header-wins")
    bus = api.app.state.services.events
    first = await bus.publish(
        workspace_id=api.app.state.services.principal.workspace_id,
        type=EventType.RUN_CREATED,
        payload={"index": 1},
        run_id=run_id,
    )
    second = await bus.publish(
        workspace_id=api.app.state.services.principal.workspace_id,
        type=EventType.RUN_STATUS_CHANGED,
        payload={"index": 2},
        run_id=run_id,
    )
    ticket = (await api.client.post("/api/v1/stream/ticket", headers=api.auth)).json()["ticket"]
    monkeypatch.setattr(stream_module, "SSE_MAX_EVENTS", 1)

    response = await api.client.get(
        "/api/v1/stream",
        params={"ticket": ticket, "last_event_id": 0},
        headers={"Last-Event-ID": str(first.seq)},
    )

    assert response.status_code == 200
    assert response.headers["content-type"] == "text/event-stream; charset=utf-8"
    assert response.headers["cache-control"] == "no-cache"
    assert response.headers["x-accel-buffering"] == "no"
    assert f"id: {second.seq}" in response.text
    assert f"id: {first.seq}" not in response.text


async def test_last_event_id_query_form_replays_missed_events(
    api: Api,
    database: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id = await _run_in_workspace(api, database, name="query-replay")
    bus = api.app.state.services.events
    first = await bus.publish(
        workspace_id=api.app.state.services.principal.workspace_id,
        type=EventType.RUN_CREATED,
        payload={"index": 1},
        run_id=run_id,
    )
    second = await bus.publish(
        workspace_id=api.app.state.services.principal.workspace_id,
        type=EventType.STEP_FINISHED,
        payload={"index": 2},
        run_id=run_id,
    )
    ticket = (await api.client.post("/api/v1/stream/ticket", headers=api.auth)).json()["ticket"]
    monkeypatch.setattr(stream_module, "SSE_MAX_EVENTS", 1)

    response = await api.client.get(
        "/api/v1/stream",
        params={"ticket": ticket, "last_event_id": first.seq},
    )

    assert response.status_code == 200
    assert f"id: {second.seq}" in response.text
    assert f"id: {first.seq}" not in response.text


async def test_run_event_history_is_paginated(
    api: Api,
    database: Database,
) -> None:
    run_id = await _run_in_workspace(api, database, name="history")
    bus = api.app.state.services.events
    workspace_id = api.app.state.services.principal.workspace_id
    for index in range(3):
        await bus.publish(
            workspace_id=workspace_id,
            type=EventType.AGENT_MESSAGE,
            payload={"index": index},
            run_id=run_id,
        )

    first_page = await api.client.get(
        f"/api/v1/runs/{run_id}/events",
        params={"after": 0, "limit": 2},
        headers=api.auth,
    )
    assert first_page.status_code == 200
    first_body = first_page.json()
    assert [item["payload"]["index"] for item in first_body["items"]] == [0, 1]
    assert first_body["next_after"] == first_body["items"][-1]["seq"]

    second_page = await api.client.get(
        f"/api/v1/runs/{run_id}/events",
        params={"after": first_body["next_after"], "limit": 2},
        headers=api.auth,
    )
    assert second_page.status_code == 200
    second_body = second_page.json()
    assert [item["payload"]["index"] for item in second_body["items"]] == [2]
    assert second_body["next_after"] is None


async def test_run_event_history_hides_another_workspaces_run(
    api: Api,
    database: Database,
    session: AsyncSession,
) -> None:
    foreign = await make_chain(session, slug="foreign-history")
    await session.commit()

    response = await api.client.get(
        f"/api/v1/runs/{foreign.run.id}/events",
        headers=api.auth,
    )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


async def test_list_run_events_function_rejects_a_missing_run_directly(
    api: Api,
    database: Database,
) -> None:
    workspace_id = api.app.state.services.principal.workspace_id
    async with database.sessions() as session:
        with pytest.raises(HTTPException) as excinfo:
            await list_run_events(
                "missing-run",
                Scoped(session, workspace_id),
            )

    assert excinfo.value.status_code == 404


async def test_list_run_events_function_is_covered_directly(
    api: Api,
    database: Database,
) -> None:
    run_id = await _run_in_workspace(api, database, name="direct-history")
    bus = api.app.state.services.events
    workspace_id = api.app.state.services.principal.workspace_id
    await bus.publish(
        workspace_id=workspace_id,
        type=EventType.RUN_CREATED,
        payload={"index": 0},
        run_id=run_id,
    )
    await bus.publish(
        workspace_id=workspace_id,
        type=EventType.RUN_STATUS_CHANGED,
        payload={"index": 1},
        run_id=run_id,
    )

    async with database.sessions() as session:
        result = await list_run_events(
            run_id,
            Scoped(session, workspace_id),
            after=0,
            limit=1,
        )

    assert [item.payload["index"] for item in result.items] == [0]
    assert result.next_after == result.items[0].seq
