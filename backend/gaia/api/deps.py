"""Dependencies: the process services, the request's session, its principal.

Owned by doc 04 (``gaia/api/deps.py``). A route asks for three things and never
builds them itself:

- the session factory lives in :class:`AppServices`, opened once by the lifespan
  and reachable from :func:`services_of`;
- :func:`get_session` opens one session per request and commits it on success;
- :func:`get_scoped` hands back a :class:`Scoped`, the only way to reach a tenant
  table. It carries the workspace from the authenticated principal, so no route
  can accidentally query "everything" (ARCHITECTURE section 7, doc 03 task 5).

``get_session`` is used with ``Depends(..., scope="function")`` so FastAPI closes
the dependency's exit stack *before* the response is sent. The commit therefore
runs first, and a failed commit (a unique-constraint violation, a locked
database) becomes an error response rather than a lie the client has already
been told.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Annotated, Any

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from gaia.api.auth import AuthProvider, Principal
from gaia.config.secrets import SecretStore
from gaia.config.settings import Settings
from gaia.db.engine import session_scope
from gaia.db.models import AuditLog, Base
from gaia.db.repo import AuditLogRepo, ScopedRepo
from gaia.services.git import GitService
from gaia.services.worktrees import WorktreeService


@dataclass(frozen=True)
class AppServices:
    """What the lifespan opened: one per process, never per request."""

    settings: Settings
    engine: AsyncEngine
    sessions: async_sessionmaker[AsyncSession]
    store: SecretStore
    auth: AuthProvider
    principal: Principal
    git: GitService


def services_of(request: Request) -> AppServices:
    """Return the process services the lifespan put on ``app.state``.

    The annotated local assignment is what keeps mypy strict happy: ``app.state``
    is dynamically typed by design, and this is the one place that bridges it.
    """
    services: AppServices = request.app.state.services
    return services


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    """Open one session for the request and commit it when the route succeeds.

    Used as ``Depends(get_session, scope="function")``: the commit runs in the
    dependency teardown before the response is sent. A failure rolls back
    (``session_scope`` re-raises), so a half-written row never survives a 4xx or
    a crash.
    """
    async with session_scope(services_of(request).sessions) as session:
        yield session


def get_principal(request: Request) -> Principal:
    """Return the authenticated caller, or raise ``AuthenticationError``.

    A plain sync dependency: the token store is a blocking OS call, so FastAPI
    runs it in its threadpool rather than on the event loop.
    """
    return services_of(request).auth.authenticate(request)


class Scoped:
    """Workspace-scoped repositories for one request.

    Routes never see a raw session and a workspace id side by side, which is what
    makes "forgot the workspace filter" impossible rather than merely unlikely.
    """

    def __init__(self, session: AsyncSession, workspace_id: str) -> None:
        self.session = session
        self.workspace_id = workspace_id

    def repo[ModelT: Base](self, model: type[ModelT]) -> ScopedRepo[ModelT]:
        """Return the repository for ``model``, scoped to this request's workspace."""
        return ScopedRepo(self.session, model, self.workspace_id)


def get_scoped(
    session: Annotated[AsyncSession, Depends(get_session, scope="function")],
    principal: Annotated[Principal, Depends(get_principal)],
) -> Scoped:
    """Return this request's scoped repositories."""
    return Scoped(session, principal.workspace_id)


def get_worktree_service(
    services: Annotated[AppServices, Depends(services_of)],
    scoped: ScopedDep,
) -> WorktreeService:
    """Return a workspace-scoped worktree service for this request.

    The service is deliberately per request: it binds the request's session and
    the authenticated workspace, so a route can never ask for another tenant's
    worktrees. ``GitService`` itself is process-wide (built in the lifespan).
    """
    return WorktreeService(
        git=services.git, session=scoped.session, workspace_id=scoped.workspace_id
    )


async def append_audit_log(
    session: AsyncSession,
    workspace_id: str,
    *,
    actor_user_id: str,
    action: str,
    target: str | None = None,
    details: dict[str, Any] | None = None,
) -> AuditLog:
    """Append one audit row for a mutating route.

    The actor is ``user:<id>``, not a raw principal: the audit trail has to name
    who did something, and doc 06 only has human callers so far. ``details`` is
    a small caller-built dict; never pass a request body verbatim.
    """
    trail = AuditLogRepo(session, AuditLog, workspace_id)
    return await trail.append(
        actor=f"user:{actor_user_id}",
        action=action,
        target=target,
        details_json=details or {},
    )


#: Annotations a route spells instead of repeating ``Depends(...)``.
SessionDep = Annotated[AsyncSession, Depends(get_session, scope="function")]
PrincipalDep = Annotated[Principal, Depends(get_principal)]
ScopedDep = Annotated[Scoped, Depends(get_scoped)]
ServicesDep = Annotated[AppServices, Depends(services_of)]
WorktreeDep = Annotated[WorktreeService, Depends(get_worktree_service)]
