"""Worktree list and remove endpoints (doc 06)."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from fastapi import HTTPException

from gaia.api.auth import Principal
from gaia.api.deps import Scoped
from gaia.api.routes.worktrees import list_worktrees, remove_worktree
from gaia.db.models import Repository, Ticket, Worktree
from gaia.db.repo import ScopedRepo
from gaia.services.worktrees import WorktreeService
from tests.conftest import Api, Database
from tests.git_repo import on_disk


async def _register_repo(api: Api, path: Path) -> str:
    response = await api.client.post(
        "/api/v1/repositories",
        json={"local_path": str(path)},
        headers=api.auth,
    )
    assert response.status_code == 201
    return str(response.json()["id"])


async def _make_worktree(
    api: Api,
    database: Database,
    git_repo: Path,
) -> tuple[str, str, Path]:
    """Create a real worktree through the service, committed in its own session."""
    repo_id = await _register_repo(api, git_repo)
    ticket_id = (
        await api.client.post(
            "/api/v1/tickets",
            json={"repo_id": repo_id, "title": "A worktree ticket"},
            headers=api.auth,
        )
    ).json()["id"]
    me = (await api.client.get("/api/v1/me", headers=api.auth)).json()
    workspace_id = me["workspace_id"]

    async with database.sessions() as session:
        repo = await ScopedRepo(session, Repository, workspace_id).get(repo_id)
        ticket = await ScopedRepo(session, Ticket, workspace_id).get(ticket_id)
        assert repo is not None
        assert ticket is not None
        service = WorktreeService(
            git=api.app.state.services.git,
            session=session,
            workspace_id=workspace_id,
        )
        worktree = await service.create(repo, ticket)
        await session.commit()
        return repo_id, worktree.id, Path(worktree.path)


async def test_curl_acceptance_register_ticket_and_list_worktrees(
    api: Api,
    database: Database,
    git_repo: Path,
) -> None:
    """The doc-06 acceptance flow: register repo, create ticket, list worktrees."""
    repo_id, _, path = await _make_worktree(api, database, git_repo)

    response = await api.client.get(
        "/api/v1/worktrees",
        params={"repo_id": repo_id},
        headers=api.auth,
    )

    assert response.status_code == 200
    body = response.json()
    assert len(body["items"]) == 1
    assert body["items"][0]["path"] == str(path)


async def test_list_worktrees_includes_flags(api: Api, database: Database, git_repo: Path) -> None:
    repo_id, _, _ = await _make_worktree(api, database, git_repo)

    response = await api.client.get(
        "/api/v1/worktrees",
        params={"repo_id": repo_id},
        headers=api.auth,
    )

    assert response.status_code == 200
    body = response.json()
    assert len(body["items"]) == 1
    item = body["items"][0]
    assert item["orphan"] is False
    assert item["exists"] is True
    assert item["dirty"] is False
    assert item["tampered"] is False
    assert body["unknown_paths"] == []


async def test_list_worktrees_reports_dirty(api: Api, database: Database, git_repo: Path) -> None:
    repo_id, _, path = await _make_worktree(api, database, git_repo)
    (path / "dirty.txt").write_text("uncommitted")

    response = await api.client.get(
        "/api/v1/worktrees",
        params={"repo_id": repo_id},
        headers=api.auth,
    )

    assert response.status_code == 200
    assert response.json()["items"][0]["dirty"] is True


async def test_list_worktrees_reports_tampering_per_row(
    api: Api,
    database: Database,
    git_repo: Path,
) -> None:
    repo_id, _, path = await _make_worktree(api, database, git_repo)
    (path / ".git").write_text("gitdir: /tmp/planted-admin-dir\n")

    response = await api.client.get(
        "/api/v1/worktrees",
        params={"repo_id": repo_id},
        headers=api.auth,
    )

    assert response.status_code == 200
    item = response.json()["items"][0]
    assert item["dirty"] is None
    assert item["tampered"] is True


async def test_list_worktrees_requires_an_existing_repo(
    api: Api,
    git_repo: Path,
) -> None:
    response = await api.client.get(
        "/api/v1/worktrees",
        params={"repo_id": "00000000-0000-0000-0000-000000000000"},
        headers=api.auth,
    )

    assert response.status_code == 404


async def test_remove_worktree_removes_directory_and_keeps_row(
    api: Api,
    database: Database,
    git_repo: Path,
) -> None:
    _, worktree_id, path = await _make_worktree(api, database, git_repo)

    response = await api.client.delete(
        f"/api/v1/worktrees/{worktree_id}",
        headers=api.auth,
    )

    assert response.status_code == 200
    assert response.json()["status"] == "removed"
    assert on_disk(path).exists is False

    async with database.sessions() as session:
        row = await session.get(Worktree, worktree_id)
    assert row is not None
    assert row.status == "removed"


async def test_remove_dirty_worktree_requires_force(
    api: Api,
    database: Database,
    git_repo: Path,
) -> None:
    _, worktree_id, path = await _make_worktree(api, database, git_repo)
    (path / "dirty.txt").write_text("uncommitted")

    refused = await api.client.delete(
        f"/api/v1/worktrees/{worktree_id}",
        headers=api.auth,
    )

    assert refused.status_code == 409
    assert refused.json()["error"]["code"] == "conflict"
    assert on_disk(path).exists is True

    forced = await api.client.delete(
        f"/api/v1/worktrees/{worktree_id}",
        params={"force": True},
        headers=api.auth,
    )

    assert forced.status_code == 200
    assert on_disk(path).exists is False


async def test_remove_tampered_worktree_conflicts(
    api: Api,
    database: Database,
    git_repo: Path,
) -> None:
    _, worktree_id, path = await _make_worktree(api, database, git_repo)
    (path / ".git").write_text("gitdir: /tmp/planted-admin-dir\n")

    response = await api.client.delete(
        f"/api/v1/worktrees/{worktree_id}",
        params={"force": True},
        headers=api.auth,
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "conflict"
    assert "tampered" in response.json()["error"]["message"]


async def test_remove_missing_worktree_is_404(api: Api) -> None:
    response = await api.client.delete(
        "/api/v1/worktrees/00000000-0000-0000-0000-000000000000",
        headers=api.auth,
    )

    assert response.status_code == 404


async def test_remove_worktree_with_foreign_repo_is_refused(
    api: Api,
    database: Database,
    git_repo: Path,
) -> None:
    """F5: a child row whose parent is in another workspace must not be trusted."""
    me = (await api.client.get("/api/v1/me", headers=api.auth)).json()
    workspace_id = me["workspace_id"]

    async with database.sessions() as session:
        from gaia.db.models import Workspace

        foreign_workspace = Workspace(name="Foreign", slug="foreign-worktrees")
        session.add(foreign_workspace)
        await session.flush()
        foreign_repo = await ScopedRepo(session, Repository, foreign_workspace.id).create(
            name="foreign-repo",
            local_path=str(git_repo),
        )
        worktree = await ScopedRepo(session, Worktree, workspace_id).create(
            repo_id=foreign_repo.id,
            path=str(api.settings.worktrees_root / "foreign-child"),
            branch="gaia/foreign-child",
        )
        await session.commit()

    response = await api.client.delete(
        f"/api/v1/worktrees/{worktree.id}",
        headers=api.auth,
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "conflict"


async def test_worktree_route_functions_directly(
    api: Api,
    database: Database,
    git_repo: Path,
) -> None:
    """Call the route functions directly so branch coverage sees every path."""
    repo_id, worktree_id, _ = await _make_worktree(api, database, git_repo)

    async with database.sessions() as session:
        services = api.app.state.services
        principal: Principal = services.principal
        scoped = Scoped(session, principal.workspace_id)
        worktree_service = WorktreeService(
            git=services.git,
            session=session,
            workspace_id=principal.workspace_id,
        )

        listing = await list_worktrees(repo_id, scoped, worktree_service)
        assert len(listing.items) == 1

        removed = await remove_worktree(
            worktree_id,
            scoped,
            principal,
            worktree_service,
            force=False,
        )
        assert removed.status.value == "removed"

        with pytest.raises(HTTPException) as caught:
            await list_worktrees("missing-repository-id", scoped, worktree_service)
        assert caught.value.status_code == 404

        with pytest.raises(HTTPException) as caught:
            await remove_worktree(
                "missing-worktree-id",
                scoped,
                principal,
                worktree_service,
                force=False,
            )
        assert caught.value.status_code == 404


def _delete_tree(path: Path) -> None:
    """Remove a worktree directory behind Gaia's back (blocking, so not in a coroutine)."""
    shutil.rmtree(path)


async def test_an_orphan_worktree_is_not_reported_as_tampered(
    api: Api, database: Database, git_repo: Path
) -> None:
    """Lead review: a directory deleted behind Gaia's back is an orphan, not tampering."""
    repo_id, worktree_id, path = await _make_worktree(api, database, git_repo)
    _delete_tree(path)

    response = await api.client.get(f"/api/v1/worktrees?repo_id={repo_id}", headers=api.auth)

    assert response.status_code == 200
    (item,) = [row for row in response.json()["items"] if row["id"] == worktree_id]
    assert item["exists"] is False
    assert item["tampered"] is False
    assert item["dirty"] is None


async def test_unregistering_a_repository_with_a_live_worktree_is_refused(
    api: Api, database: Database, git_repo: Path
) -> None:
    repo_id, _worktree_id, _path = await _make_worktree(api, database, git_repo)

    response = await api.client.delete(f"/api/v1/repositories/{repo_id}", headers=api.auth)

    assert response.status_code == 409
