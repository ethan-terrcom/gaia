"""Repository endpoints (doc 06)."""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import select

from gaia.db.models import AuditLog, Repository, Run, Worktree
from gaia.db.repo import ScopedRepo
from gaia.domain.enums import RunStatus, WorktreeStatus
from tests.conftest import Api, Database
from tests.db_builders import make_workspace
from tests.git_repo import git, make_repo, on_disk, resolved


async def test_register_repository_detects_name_and_branch(api: Api, git_repo: Path) -> None:
    response = await api.client.post(
        "/api/v1/repositories",
        json={"local_path": str(git_repo)},
        headers=api.auth,
    )

    assert response.status_code == 201
    body = response.json()
    assert body["name"] == git_repo.name
    assert body["local_path"] == str(resolved(git_repo))
    assert body["default_branch"] == "main"
    assert body["remote_url"] is None
    assert body["setup_command"] is None
    assert body["test_command"] is None


async def test_register_repository_detects_origin(api: Api, git_repo: Path) -> None:
    git(git_repo, "remote", "add", "origin", "https://example.invalid/team/repo.git")

    response = await api.client.post(
        "/api/v1/repositories",
        json={"local_path": str(git_repo), "name": "with-origin"},
        headers=api.auth,
    )

    assert response.status_code == 201
    assert response.json()["remote_url"] == "https://example.invalid/team/repo.git"


async def test_register_repository_accepts_argv_commands(api: Api, git_repo: Path) -> None:
    response = await api.client.post(
        "/api/v1/repositories",
        json={
            "local_path": str(git_repo),
            "setup_command": ["uv", "sync", "--locked"],
            "test_command": ["uv", "run", "pytest"],
        },
        headers=api.auth,
    )

    assert response.status_code == 201
    body = response.json()
    assert body["setup_command"] == ["uv", "sync", "--locked"]
    assert body["test_command"] == ["uv", "run", "pytest"]


async def test_register_repository_refuses_a_non_git_path(api: Api, tmp_path: Path) -> None:
    not_a_repo = tmp_path / "not-a-repo"
    not_a_repo.mkdir()

    response = await api.client.post(
        "/api/v1/repositories",
        json={"local_path": str(not_a_repo)},
        headers=api.auth,
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"
    assert "requested git operation" in response.json()["error"]["message"]
    assert str(not_a_repo) not in response.text


async def test_register_repository_rejects_unknown_fields(api: Api, git_repo: Path) -> None:
    response = await api.client.post(
        "/api/v1/repositories",
        json={"local_path": str(git_repo), "remote_url": "https://example.invalid/repo.git"},
        headers=api.auth,
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


async def test_register_repository_rejects_invalid_commands(api: Api, git_repo: Path) -> None:
    response = await api.client.post(
        "/api/v1/repositories",
        json={"local_path": str(git_repo), "setup_command": []},
        headers=api.auth,
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


async def test_duplicate_name_conflicts(api: Api, tmp_path: Path) -> None:
    first_repo = make_repo(tmp_path / "first")
    second_repo = make_repo(tmp_path / "second")

    first = await api.client.post(
        "/api/v1/repositories",
        json={"local_path": str(first_repo), "name": "duplicate-name"},
        headers=api.auth,
    )
    second = await api.client.post(
        "/api/v1/repositories",
        json={"local_path": str(second_repo), "name": "duplicate-name"},
        headers=api.auth,
    )

    assert first.status_code == 201
    assert second.status_code == 409
    assert second.json()["error"]["code"] == "conflict"


async def test_duplicate_path_conflicts(api: Api, git_repo: Path) -> None:
    first = await api.client.post(
        "/api/v1/repositories",
        json={"local_path": str(git_repo), "name": "first-name"},
        headers=api.auth,
    )
    second = await api.client.post(
        "/api/v1/repositories",
        json={"local_path": str(git_repo), "name": "second-name"},
        headers=api.auth,
    )

    assert first.status_code == 201
    assert second.status_code == 409


async def test_list_repositories_returns_only_this_workspace(
    api: Api,
    database: Database,
    git_repo: Path,
) -> None:
    created = await api.client.post(
        "/api/v1/repositories",
        json={"local_path": str(git_repo)},
        headers=api.auth,
    )
    created_id = created.json()["id"]

    async with database.sessions() as session:
        workspace = await make_workspace(session, slug="foreign-repositories")
        foreign_repo = await ScopedRepo(session, Repository, workspace.id).create(
            name="foreign-repo",
            local_path="/tmp/foreign-repo",
        )
        await session.commit()

    response = await api.client.get("/api/v1/repositories", headers=api.auth)

    assert response.status_code == 200
    assert [row["id"] for row in response.json()] == [created_id]
    assert foreign_repo.id not in [row["id"] for row in response.json()]


async def test_get_repository_hides_a_foreign_row(
    api: Api,
    database: Database,
) -> None:
    async with database.sessions() as session:
        workspace = await make_workspace(session, slug="foreign-get")
        foreign_repo = await ScopedRepo(session, Repository, workspace.id).create(
            name="foreign-repo",
            local_path="/tmp/foreign-get",
        )
        await session.commit()

    response = await api.client.get(
        f"/api/v1/repositories/{foreign_repo.id}",
        headers=api.auth,
    )

    assert response.status_code == 404


async def test_get_repository_includes_worktree_and_run_counts(
    api: Api,
    database: Database,
    git_repo: Path,
) -> None:
    created = await api.client.post(
        "/api/v1/repositories",
        json={"local_path": str(git_repo)},
        headers=api.auth,
    )
    repo_id = created.json()["id"]
    me = (await api.client.get("/api/v1/me", headers=api.auth)).json()

    ticket_response = await api.client.post(
        "/api/v1/tickets",
        json={"repo_id": repo_id, "title": "Count runs"},
        headers=api.auth,
    )
    ticket_id = ticket_response.json()["id"]

    async with database.sessions() as session:
        worktrees = ScopedRepo(session, Worktree, me["workspace_id"])
        await worktrees.create(
            repo_id=repo_id,
            path="/tmp/active-worktree",
            branch="gaia/active",
        )
        await worktrees.create(
            repo_id=repo_id,
            path="/tmp/removed-worktree",
            branch="gaia/removed",
            status=WorktreeStatus.REMOVED.value,
        )
        runs = ScopedRepo(session, Run, me["workspace_id"])
        await runs.create(
            ticket_id=ticket_id,
            repo_id=repo_id,
            status=RunStatus.PENDING.value,
        )
        await runs.create(
            ticket_id=ticket_id,
            repo_id=repo_id,
            status=RunStatus.COMPLETED.value,
        )
        await session.commit()

    response = await api.client.get(f"/api/v1/repositories/{repo_id}", headers=api.auth)

    assert response.status_code == 200
    body = response.json()
    assert body["worktree_count"] == 1
    assert body["active_runs"] == 1


async def test_delete_repository_unregisters_only_the_row(
    api: Api,
    git_repo: Path,
) -> None:
    created = await api.client.post(
        "/api/v1/repositories",
        json={"local_path": str(git_repo)},
        headers=api.auth,
    )
    repo_id = created.json()["id"]

    response = await api.client.delete(
        f"/api/v1/repositories/{repo_id}",
        headers=api.auth,
    )

    assert response.status_code == 204
    assert on_disk(git_repo).exists

    fetched = await api.client.get(f"/api/v1/repositories/{repo_id}", headers=api.auth)
    assert fetched.status_code == 404


async def test_mutating_repository_routes_write_audit_rows(
    api: Api,
    database: Database,
    git_repo: Path,
) -> None:
    created = await api.client.post(
        "/api/v1/repositories",
        json={"local_path": str(git_repo)},
        headers=api.auth,
    )
    repo_id = created.json()["id"]

    await api.client.delete(f"/api/v1/repositories/{repo_id}", headers=api.auth)

    async with database.sessions() as session:
        rows = list((await session.scalars(select(AuditLog))).all())

    assert {row.action for row in rows} == {"repository.register", "repository.unregister"}
    assert all(row.actor.startswith("user:") for row in rows)
    assert all(row.target == repo_id for row in rows)


async def test_register_repository_uses_audit_actor(
    api: Api,
    database: Database,
    git_repo: Path,
) -> None:
    """The actor is the authenticated principal, not a hard-coded string."""
    me = (await api.client.get("/api/v1/me", headers=api.auth)).json()
    await api.client.post(
        "/api/v1/repositories",
        json={"local_path": str(git_repo)},
        headers=api.auth,
    )

    async with database.sessions() as session:
        row = (await session.scalars(select(AuditLog))).one()

    assert row.actor == f"user:{me['user_id']}"


async def test_unregistering_a_repository_with_tickets_is_refused(api: Api, git_repo: Path) -> None:
    """Lead review: the row cascades, so unregistering would delete ticket history."""
    repo_id = (
        await api.client.post(
            "/api/v1/repositories", json={"local_path": str(git_repo)}, headers=api.auth
        )
    ).json()["id"]
    await api.client.post(
        "/api/v1/tickets", json={"repo_id": repo_id, "title": "Keep me"}, headers=api.auth
    )

    response = await api.client.delete(f"/api/v1/repositories/{repo_id}", headers=api.auth)

    assert response.status_code == 409
    assert "history" in response.json()["error"]["message"]
    still_there = await api.client.get(f"/api/v1/repositories/{repo_id}", headers=api.auth)
    assert still_there.status_code == 200


async def test_register_repository_strips_credentials_from_origin(api: Api, git_repo: Path) -> None:
    """Lead review: a token embedded in a remote URL is never stored or returned."""
    git(git_repo, "remote", "add", "origin", "https://bot:not-a-real-token@example.invalid/r.git")

    response = await api.client.post(
        "/api/v1/repositories", json={"local_path": str(git_repo)}, headers=api.auth
    )

    assert response.status_code == 201
    assert response.json()["remote_url"] == "https://example.invalid/r.git"
