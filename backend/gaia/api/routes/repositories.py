"""Repository registration and un-registration (doc 06).

Registering is deliberately read-only on the filesystem: the route asks git to
validate the path, detects the remote and default branch, and then writes a
``repository`` row. Deleting removes only the row - never the directory, and
never the owner's git repo.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, status

from gaia.api.deps import PrincipalDep, ScopedDep, ServicesDep, append_audit_log
from gaia.db.models import Repository, Run, Ticket, Worktree
from gaia.domain.enums import RunStatus, WorktreeStatus
from gaia.domain.schemas import RepositoryCreate, RepositoryDetailRead, RepositoryRead

router = APIRouter(tags=["repositories"])


@router.post(
    "/repositories",
    response_model=RepositoryRead,
    status_code=status.HTTP_201_CREATED,
    summary="Register a local git repository",
)
async def register_repository(
    payload: RepositoryCreate,
    scoped: ScopedDep,
    principal: PrincipalDep,
    services: ServicesDep,
) -> RepositoryRead:
    """Validate ``local_path``, detect its origin and default branch, and add a row."""
    repo_path = await services.git.validate_repo(payload.local_path)
    remote_url = await services.git.remote_url(repo_path)
    default_branch = await services.git.default_branch(repo_path)
    name = payload.name or repo_path.name

    repositories = scoped.repo(Repository)
    duplicate = await repositories.list(name=name) or await repositories.list(
        local_path=str(repo_path)
    )
    if duplicate:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "a repository with this name or path is already registered",
        )

    repository = await repositories.create(
        name=name,
        local_path=str(repo_path),
        remote_url=remote_url,
        default_branch=default_branch,
        setup_command=payload.setup_command,
        test_command=payload.test_command,
        created_by=principal.user_id,
    )
    await append_audit_log(
        scoped.session,
        scoped.workspace_id,
        actor_user_id=principal.user_id,
        action="repository.register",
        target=repository.id,
        details={"name": name},
    )
    return RepositoryRead.model_validate(repository)


@router.get("/repositories", response_model=list[RepositoryRead], summary="List repositories")
async def list_repositories(scoped: ScopedDep) -> list[RepositoryRead]:
    """Return every repository registered in this workspace."""
    rows = await scoped.repo(Repository).list()
    return [RepositoryRead.model_validate(row) for row in rows]


@router.get(
    "/repositories/{repository_id}",
    response_model=RepositoryDetailRead,
    summary="Get one repository",
)
async def get_repository(repository_id: str, scoped: ScopedDep) -> RepositoryDetailRead:
    """Return the repository plus its worktree count and active run count."""
    repository = await scoped.repo(Repository).get(repository_id)
    if repository is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "repository not found")

    worktrees = await scoped.repo(Worktree).list(repo_id=repository.id)
    worktree_count = sum(
        1 for worktree in worktrees if worktree.status != WorktreeStatus.REMOVED.value
    )

    runs = await scoped.repo(Run).list(repo_id=repository.id)
    terminal = {RunStatus.COMPLETED.value, RunStatus.FAILED.value, RunStatus.CANCELLED.value}
    active_runs = sum(1 for run in runs if run.status not in terminal)

    base = RepositoryRead.model_validate(repository).model_dump()
    return RepositoryDetailRead.model_validate(
        {**base, "worktree_count": worktree_count, "active_runs": active_runs}
    )


@router.delete(
    "/repositories/{repository_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Unregister a repository",
)
async def unregister_repository(
    repository_id: str,
    scoped: ScopedDep,
    principal: PrincipalDep,
) -> None:
    """Delete the row only. No file or directory on disk is touched.

    Refused with 409 while the repository has tickets or live worktrees: the
    foreign keys cascade, so deleting the row would silently delete every
    ticket, run, approval and usage record that hangs off it (lead review of 06).
    """
    repositories = scoped.repo(Repository)
    repository = await repositories.get(repository_id)
    if repository is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "repository not found")
    has_tickets = bool(await scoped.repo(Ticket).list(repo_id=repository_id, limit=1))
    live_worktrees = [
        row
        for row in await scoped.repo(Worktree).list(repo_id=repository_id)
        if row.status != WorktreeStatus.REMOVED.value
    ]
    if has_tickets or live_worktrees:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "the repository still has tickets or worktrees; "
            "unregistering it would delete their history",
        )
    await repositories.delete(repository_id)
    await append_audit_log(
        scoped.session,
        scoped.workspace_id,
        actor_user_id=principal.user_id,
        action="repository.unregister",
        target=repository_id,
        details={"name": repository.name},
    )
