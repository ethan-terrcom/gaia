"""Dependencies: the request session, the principal and the scoped repos (doc 04).

``Scoped`` is the only door to a tenant table (doc 03 task 5), so the two things
worth proving are that it really hides another workspace and that the workspace it
carries is the authenticated principal's, not a constant or a request parameter.
"""

from __future__ import annotations

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from gaia.api.app import create_app
from gaia.api.deps import Scoped, ScopedDep
from gaia.db.models import Agent, Ticket, User
from gaia.domain.enums import AgentRole, RuntimeKind
from tests.conftest import API_BASE_URL, Api, Database
from tests.db_builders import make_chain


async def test_a_scoped_repo_cannot_read_another_workspaces_row(
    session: AsyncSession,
) -> None:
    ours = await make_chain(session, "ours")
    theirs = await make_chain(session, "theirs")
    tickets = Scoped(session, ours.workspace.id).repo(Ticket)

    assert await tickets.get(theirs.ticket.id) is None
    assert await tickets.get(ours.ticket.id) is not None
    assert [ticket.title for ticket in await tickets.list()] == [ours.ticket.title]


async def test_a_scoped_write_lands_in_the_principals_workspace(
    api: Api, database: Database
) -> None:
    """One route that authenticates and writes proves the whole chain.

    It also pins the session's commit timing: the row is readable from a *new*
    session after the response, which is what "committed" means to doc 06.
    """

    async def create_agent(scoped: ScopedDep) -> dict[str, str]:
        agent = await scoped.repo(Agent).create(
            role=AgentRole.JUNIOR,
            runtime=RuntimeKind.NATIVE_DEEPSEEK,
            model="deepseek-v4-pro",
            display_name="junior-under-test",
        )
        return {"id": agent.id, "workspace_id": agent.workspace_id}

    me = (await api.client.get("/api/v1/me", headers=api.auth)).json()

    app = create_app(api.settings)
    app.add_api_route("/api/v1/_agents", create_agent, methods=["POST"])
    transport = httpx.ASGITransport(app=app)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=transport, base_url=API_BASE_URL) as client:
            anonymous = await client.post("/api/v1/_agents")
            created = await client.post("/api/v1/_agents", headers=api.auth)

    assert anonymous.status_code == 401
    assert created.status_code == 200
    assert created.json()["workspace_id"] == me["workspace_id"]

    async with database.sessions() as session:
        agent = await session.get(Agent, created.json()["id"])

    assert agent is not None
    assert agent.display_name == "junior-under-test"


async def test_a_failed_commit_before_the_response_is_a_409(api: Api) -> None:
    """F6: the dependency commit now runs before the response is sent.

    The route adds a duplicate user but never flushes; the unique violation
    happens when ``get_session`` commits in teardown. The client must receive
    the conflict, not a 2xx that would already have left.
    """

    async def duplicate_user(scoped: ScopedDep) -> None:
        # Deliberately no flush: the failure is the commit in the dependency
        # teardown, which is exactly what F6 changed to run before the response.
        scoped.session.add(
            User(
                workspace_id=scoped.workspace_id,
                email="owner@localhost",
                display_name="duplicate",
            )
        )

    app = create_app(api.settings)
    app.add_api_route("/api/v1/_duplicate-user", duplicate_user, methods=["POST"])
    transport = httpx.ASGITransport(app=app)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=transport, base_url=API_BASE_URL) as client:
            response = await client.post("/api/v1/_duplicate-user", headers=api.auth)

    assert response.status_code == 409
    error = response.json()["error"]
    assert error["code"] == "conflict"
    assert "owner@localhost" not in response.text
    assert "INSERT" not in response.text
    assert "UNIQUE" not in response.text
