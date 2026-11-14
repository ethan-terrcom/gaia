"""Worktree listing and removal for the dashboard (doc 06).

Listing is read-only; it reports orphans and unknown paths from
:class:`~gaia.services.worktrees.WorktreeService` and adds the per-row ``dirty``
flag that doc 05 deliberately left out of ``list()``. If a worktree has been
tampered with, its row reports ``dirty: null, tampered: true`` instead of
failing the whole list.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, status

from gaia.api.deps import PrincipalDep, ScopedDep, WorktreeDep, append_audit_log
from gaia.db.models import Repository, Worktree
from gaia.domain.schemas import WorktreeItemRead, WorktreeListRead, WorktreeRead
from gaia.services.git import WorktreeTampered

router = APIRouter(tags=["worktrees"])


@router.get("/worktrees", response_model=WorktreeListRead, summary="List worktrees")
async def list_worktrees(
    repo_id: str,
    scoped: ScopedDep,
    worktrees: WorktreeDep,
) -> WorktreeListRead:
    """Return a repository's worktrees with orphan and dirty flags."""
    repository = await scoped.repo(Repository).get(repo_id)
    if repository is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "repository not found")

    report = await worktrees.list(repository)
    items: list[WorktreeItemRead] = []
    for item in report.items:
        dirty: bool | None = None
        tampered = False
        # An orphan (deleted behind Gaia's back, or unknown to git) has no
        # `.git` file to check and no tree to be dirty: report it as an orphan,
        # not as tampered (lead review of 06).
        if item.exists and item.in_git:
            try:
                location = await worktrees.location(item.worktree)
                dirty = await worktrees.git.is_dirty(location)
            except WorktreeTampered:
                tampered = True
        base = WorktreeRead.model_validate(item.worktree).model_dump()
        items.append(
            WorktreeItemRead.model_validate(
                {
                    **base,
                    "orphan": item.orphan,
                    "exists": item.exists,
                    "dirty": dirty,
                    "tampered": tampered,
                }
            )
        )
    return WorktreeListRead(
        items=items,
        unknown_paths=[str(path) for path in report.unknown_paths],
    )


@router.delete(
    "/worktrees/{worktree_id}",
    response_model=WorktreeRead,
    summary="Remove a worktree",
)
async def remove_worktree(
    worktree_id: str,
    scoped: ScopedDep,
    principal: PrincipalDep,
    worktrees: WorktreeDep,
    force: Annotated[bool, Query()] = False,
) -> WorktreeRead:
    """Remove a worktree from disk and mark its row ``removed``.

    The row is kept as history. A dirty worktree is refused unless ``force`` is
    set; a tampered worktree is always refused.
    """
    row = await scoped.repo(Worktree).get(worktree_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "worktree not found")
    removed = await worktrees.remove(row, force=force)
    await append_audit_log(
        scoped.session,
        scoped.workspace_id,
        actor_user_id=principal.user_id,
        action="worktree.remove",
        target=removed.id,
        details={"force": force},
    )
    return WorktreeRead.model_validate(removed)
