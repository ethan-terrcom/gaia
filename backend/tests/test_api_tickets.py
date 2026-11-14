"""Ticket endpoints and the board query (doc 06)."""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import select

from gaia.db.models import AuditLog, Repository, Ticket
from gaia.db.repo import ScopedRepo
from tests.conftest import Api, Database
from tests.db_builders import make_workspace


async def _register_repo(api: Api, path: Path, *, name: str = "repo") -> str:
    response = await api.client.post(
        "/api/v1/repositories",
        json={"local_path": str(path), "name": name},
        headers=api.auth,
    )
    assert response.status_code == 201
    return str(response.json()["id"])


async def test_create_ticket_returns_backlog_ticket(api: Api, git_repo: Path) -> None:
    repo_id = await _register_repo(api, git_repo)

    response = await api.client.post(
        "/api/v1/tickets",
        json={
            "repo_id": repo_id,
            "title": "Ship the dashboard",
            "body": "A body",
            "priority": 2,
            "external_ref": "issue-1",
        },
        headers=api.auth,
    )

    assert response.status_code == 201
    body = response.json()
    assert body["repo_id"] == repo_id
    assert body["title"] == "Ship the dashboard"
    assert body["body"] == "A body"
    assert body["status"] == "backlog"
    assert body["priority"] == 2
    assert body["external_ref"] == "issue-1"


async def test_create_ticket_loads_parent_in_the_callers_workspace(
    api: Api,
    database: Database,
) -> None:
    async with database.sessions() as session:
        workspace = await make_workspace(session, slug="foreign-tickets")
        foreign_repo = await ScopedRepo(session, Repository, workspace.id).create(
            name="foreign-repo",
            local_path="/tmp/foreign-tickets",
        )
        await session.commit()

    response = await api.client.post(
        "/api/v1/tickets",
        json={"repo_id": foreign_repo.id, "title": "Hijack"},
        headers=api.auth,
    )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


async def test_create_ticket_rejects_unknown_fields(api: Api, git_repo: Path) -> None:
    repo_id = await _register_repo(api, git_repo)

    response = await api.client.post(
        "/api/v1/tickets",
        json={"repo_id": repo_id, "title": "No status allowed", "status": "done"},
        headers=api.auth,
    )

    assert response.status_code == 422


async def test_ticket_title_and_body_limits_are_enforced(api: Api, git_repo: Path) -> None:
    repo_id = await _register_repo(api, git_repo)

    too_long_title = await api.client.post(
        "/api/v1/tickets",
        json={"repo_id": repo_id, "title": "x" * 201},
        headers=api.auth,
    )
    too_long_body = await api.client.post(
        "/api/v1/tickets",
        json={"repo_id": repo_id, "title": "ok", "body": "x" * 50_001},
        headers=api.auth,
    )

    assert too_long_title.status_code == 422
    assert too_long_body.status_code == 422


async def test_get_ticket_hides_another_workspaces_ticket(
    api: Api,
    database: Database,
) -> None:
    async with database.sessions() as session:
        workspace = await make_workspace(session, slug="foreign-ticket-get")
        foreign_repo = await ScopedRepo(session, Repository, workspace.id).create(
            name="foreign-repo",
            local_path="/tmp/foreign-ticket-get",
        )
        foreign_ticket = await ScopedRepo(session, Ticket, workspace.id).create(
            repo_id=foreign_repo.id,
            title="Foreign ticket",
        )
        await session.commit()

    response = await api.client.get(
        f"/api/v1/tickets/{foreign_ticket.id}",
        headers=api.auth,
    )

    assert response.status_code == 404


async def test_patch_ticket_updates_text_fields(api: Api, git_repo: Path) -> None:
    repo_id = await _register_repo(api, git_repo)
    ticket_id = (
        await api.client.post(
            "/api/v1/tickets",
            json={"repo_id": repo_id, "title": "Old title"},
            headers=api.auth,
        )
    ).json()["id"]

    response = await api.client.patch(
        f"/api/v1/tickets/{ticket_id}",
        json={"title": "New title", "priority": 3},
        headers=api.auth,
    )

    assert response.status_code == 200
    body = response.json()
    assert body["title"] == "New title"
    assert body["priority"] == 3
    assert body["status"] == "backlog"


async def test_patch_ticket_legal_transition(api: Api, git_repo: Path) -> None:
    repo_id = await _register_repo(api, git_repo)
    ticket_id = (
        await api.client.post(
            "/api/v1/tickets",
            json={"repo_id": repo_id, "title": "Move to ready"},
            headers=api.auth,
        )
    ).json()["id"]

    response = await api.client.patch(
        f"/api/v1/tickets/{ticket_id}",
        json={"status": "ready"},
        headers=api.auth,
    )

    assert response.status_code == 200
    assert response.json()["status"] == "ready"


async def test_patch_ticket_illegal_transition_conflicts(api: Api, git_repo: Path) -> None:
    repo_id = await _register_repo(api, git_repo)
    ticket_id = (
        await api.client.post(
            "/api/v1/tickets",
            json={"repo_id": repo_id, "title": "Too fast"},
            headers=api.auth,
        )
    ).json()["id"]

    response = await api.client.patch(
        f"/api/v1/tickets/{ticket_id}",
        json={"status": "done"},
        headers=api.auth,
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "conflict"


async def test_patch_ticket_rejects_an_empty_body(api: Api, git_repo: Path) -> None:
    repo_id = await _register_repo(api, git_repo)
    ticket_id = (
        await api.client.post(
            "/api/v1/tickets",
            json={"repo_id": repo_id, "title": "Empty patch"},
            headers=api.auth,
        )
    ).json()["id"]

    response = await api.client.patch(
        f"/api/v1/tickets/{ticket_id}",
        json={},
        headers=api.auth,
    )

    assert response.status_code == 422


async def test_pagination_returns_every_ticket_once(api: Api, git_repo: Path) -> None:
    repo_id = await _register_repo(api, git_repo)
    created_ids = []
    for index in range(5):
        response = await api.client.post(
            "/api/v1/tickets",
            json={"repo_id": repo_id, "title": f"Ticket {index}"},
            headers=api.auth,
        )
        created_ids.append(response.json()["id"])

    seen: list[str] = []
    cursor: str | None = None
    while True:
        params: dict[str, str] = {"limit": "2"}
        if cursor is not None:
            params["cursor"] = cursor
        response = await api.client.get("/api/v1/tickets", params=params, headers=api.auth)
        assert response.status_code == 200
        body = response.json()
        seen.extend(item["id"] for item in body["items"])
        cursor = body["next_cursor"]
        if cursor is None:
            break
        assert len(body["items"]) == 2

    assert seen == created_ids


async def test_ticket_filters(api: Api, git_repo: Path) -> None:
    repo_id = await _register_repo(api, git_repo)
    await api.client.post(
        "/api/v1/tickets",
        json={"repo_id": repo_id, "title": "Fix alpha bug"},
        headers=api.auth,
    )
    beta = await api.client.post(
        "/api/v1/tickets",
        json={"repo_id": repo_id, "title": "Update beta docs"},
        headers=api.auth,
    )
    await api.client.patch(
        f"/api/v1/tickets/{beta.json()['id']}",
        json={"status": "ready"},
        headers=api.auth,
    )

    by_repo = await api.client.get("/api/v1/tickets", params={"repo_id": repo_id}, headers=api.auth)
    by_status = await api.client.get(
        "/api/v1/tickets", params={"status": "ready"}, headers=api.auth
    )
    by_q = await api.client.get("/api/v1/tickets", params={"q": "ALPHA"}, headers=api.auth)

    assert len(by_repo.json()["items"]) == 2
    assert [item["title"] for item in by_status.json()["items"]] == ["Update beta docs"]
    assert [item["title"] for item in by_q.json()["items"]] == ["Fix alpha bug"]


async def test_ticket_search_escapes_like_wildcards(api: Api, git_repo: Path) -> None:
    repo_id = await _register_repo(api, git_repo)
    await api.client.post(
        "/api/v1/tickets",
        json={"repo_id": repo_id, "title": "100% ready"},
        headers=api.auth,
    )
    await api.client.post(
        "/api/v1/tickets",
        json={"repo_id": repo_id, "title": "100_ready"},
        headers=api.auth,
    )
    await api.client.post(
        "/api/v1/tickets",
        json={"repo_id": repo_id, "title": "anything else"},
        headers=api.auth,
    )

    percent = await api.client.get("/api/v1/tickets", params={"q": "%"}, headers=api.auth)
    underscore = await api.client.get("/api/v1/tickets", params={"q": "_"}, headers=api.auth)

    assert [item["title"] for item in percent.json()["items"]] == ["100% ready"]
    assert [item["title"] for item in underscore.json()["items"]] == ["100_ready"]


async def test_mutating_ticket_routes_write_audit_rows(
    api: Api,
    database: Database,
    git_repo: Path,
) -> None:
    repo_id = await _register_repo(api, git_repo)
    created = await api.client.post(
        "/api/v1/tickets",
        json={"repo_id": repo_id, "title": "Audit me"},
        headers=api.auth,
    )
    ticket_id = created.json()["id"]
    await api.client.patch(
        f"/api/v1/tickets/{ticket_id}",
        json={"status": "ready"},
        headers=api.auth,
    )

    async with database.sessions() as session:
        rows = list((await session.scalars(select(AuditLog))).all())

    assert {row.action for row in rows} == {"repository.register", "ticket.create", "ticket.update"}
    assert sum(row.action == "ticket.update" for row in rows) == 1
