"""Direct calls to route functions for branch coverage (doc 06).

FastAPI dependency injection and coverage's branch tracing occasionally disagree
about which lines an HTTP request executes, so the route functions are also
called directly with the same objects the dependencies build. This is not a
second API contract; it is a unit test that makes the coverage report honest.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from fastapi import HTTPException

from gaia.api.auth import Principal
from gaia.api.deps import AppServices, Scoped
from gaia.api.routes.repositories import (
    get_repository,
    list_repositories,
    register_repository,
    unregister_repository,
)
from gaia.api.routes.tickets import (
    create_ticket,
    get_ticket,
    list_tickets,
    update_ticket,
)
from gaia.domain.enums import TicketStatus
from gaia.domain.schemas import RepositoryCreate, TicketCreate, TicketPatch
from tests.conftest import Api, Database


@asynccontextmanager
async def _scoped_for_api(
    database: Database,
    api: Api,
) -> AsyncIterator[tuple[Scoped, Principal, AppServices]]:
    """Open a session on the API's database and commit on success.

    ``database.sessions()`` opens a session but does not commit; direct route
    calls must still leave the row visible to the API after the block.
    """
    async with database.sessions() as session:
        services: AppServices = api.app.state.services
        principal = services.principal
        try:
            yield Scoped(session, principal.workspace_id), principal, services
        except BaseException:
            await session.rollback()
            raise
        else:
            await session.commit()


async def test_register_repository_function_success(
    api: Api,
    database: Database,
    git_repo: Path,
) -> None:
    async with _scoped_for_api(database, api) as (scoped, principal, services):
        repository = await register_repository(
            RepositoryCreate(local_path=str(git_repo), name="direct-success"),
            scoped,
            principal,
            services,
        )
        assert repository.name == "direct-success"


async def test_register_repository_function_duplicate_conflicts(
    api: Api,
    database: Database,
    git_repo: Path,
) -> None:
    await api.client.post(
        "/api/v1/repositories",
        json={"local_path": str(git_repo), "name": "direct-duplicate"},
        headers=api.auth,
    )

    async with _scoped_for_api(database, api) as (scoped, principal, services):
        with pytest.raises(HTTPException) as caught:
            await register_repository(
                RepositoryCreate(local_path=str(git_repo), name="other-name"),
                scoped,
                principal,
                services,
            )
        assert caught.value.status_code == 409


async def test_repository_read_functions_directly(
    api: Api,
    database: Database,
    git_repo: Path,
) -> None:
    created = await api.client.post(
        "/api/v1/repositories",
        json={"local_path": str(git_repo), "name": "direct-read"},
        headers=api.auth,
    )
    repo_id = str(created.json()["id"])

    async with _scoped_for_api(database, api) as (scoped, _, _):
        rows = await list_repositories(scoped)
        assert [row.id for row in rows] == [repo_id]

        detail = await get_repository(repo_id, scoped)
        assert detail.id == repo_id

        with pytest.raises(HTTPException) as caught:
            await get_repository("missing-repository-id", scoped)
        assert caught.value.status_code == 404


async def test_unregister_repository_function_success(
    api: Api,
    database: Database,
    git_repo: Path,
) -> None:
    created = await api.client.post(
        "/api/v1/repositories",
        json={"local_path": str(git_repo), "name": "direct-unregister"},
        headers=api.auth,
    )
    repo_id = str(created.json()["id"])

    async with _scoped_for_api(database, api) as (scoped, principal, _):
        await unregister_repository(repo_id, scoped, principal)

    fetched = await api.client.get(f"/api/v1/repositories/{repo_id}", headers=api.auth)
    assert fetched.status_code == 404


async def test_unregister_repository_function_missing(
    api: Api,
    database: Database,
) -> None:
    async with _scoped_for_api(database, api) as (scoped, principal, _):
        with pytest.raises(HTTPException) as caught:
            await unregister_repository("missing-repository-id", scoped, principal)
        assert caught.value.status_code == 404


async def test_unregister_repository_function_refuses_one_with_tickets(
    api: Api,
    database: Database,
    git_repo: Path,
) -> None:
    repo_id = (
        await api.client.post(
            "/api/v1/repositories",
            json={"local_path": str(git_repo), "name": "direct-has-tickets"},
            headers=api.auth,
        )
    ).json()["id"]
    await api.client.post(
        "/api/v1/tickets", json={"repo_id": repo_id, "title": "Keep me"}, headers=api.auth
    )

    async with _scoped_for_api(database, api) as (scoped, principal, _):
        with pytest.raises(HTTPException) as caught:
            await unregister_repository(repo_id, scoped, principal)
        assert caught.value.status_code == 409


async def test_ticket_route_functions_directly(
    api: Api,
    database: Database,
    git_repo: Path,
) -> None:
    repo_id = (
        await api.client.post(
            "/api/v1/repositories",
            json={"local_path": str(git_repo), "name": "tickets-direct"},
            headers=api.auth,
        )
    ).json()["id"]

    async with _scoped_for_api(database, api) as (scoped, principal, _):
        created = await create_ticket(
            TicketCreate(repo_id=repo_id, title="Direct ticket"),
            scoped,
            principal,
        )
        assert created.status.value == "backlog"

        page = await list_tickets(scoped)
        assert len(page.items) == 1

        fetched = await get_ticket(created.id, scoped)
        assert fetched.id == created.id

        updated = await update_ticket(
            created.id,
            TicketPatch(title="Updated title"),
            scoped,
            principal,
        )
        assert updated.title == "Updated title"

        moved = await update_ticket(
            created.id,
            TicketPatch(status=TicketStatus.READY),
            scoped,
            principal,
        )
        assert moved.status.value == "ready"

        with pytest.raises(HTTPException) as caught:
            await get_ticket("missing-ticket-id", scoped)
        assert caught.value.status_code == 404

        with pytest.raises(HTTPException) as caught:
            await update_ticket(
                "missing-ticket-id",
                TicketPatch(title="No such ticket"),
                scoped,
                principal,
            )
        assert caught.value.status_code == 404


async def test_create_ticket_function_parent_not_found(
    api: Api,
    database: Database,
) -> None:
    async with _scoped_for_api(database, api) as (scoped, principal, _):
        with pytest.raises(HTTPException) as caught:
            await create_ticket(
                TicketCreate(repo_id="missing-repository-id", title="Orphan"),
                scoped,
                principal,
            )
        assert caught.value.status_code == 404
