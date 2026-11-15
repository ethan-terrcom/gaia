"""Real implementations of the orchestrator's execution protocols (doc 12b-1).

Each protocol lives in its own module and takes every collaborator in
``__init__``, keyword-only, so 12b-3 can wire the app and tests can inject
fakes. There is deliberately no module-level state here.
"""

from __future__ import annotations

from pathlib import Path

from gaia.config.settings import Settings
from gaia.db.models import Worktree
from gaia.services.git import GitService, WorktreeLocation, resolve_within
from gaia.tools.shell import SrtCommandRunner


async def worktree_location(
    *,
    git: GitService,
    repo_path: Path,
    worktree: Worktree,
) -> WorktreeLocation:
    """Build the pinned :class:`WorktreeLocation` for ``worktree``.

    ``repo_path`` is the repository root, already validated by the caller. The
    caller loads the database rows first and closes its session before calling
    this, because deriving the admin directory can run git.
    """
    work_tree = resolve_within(git.worktrees_root, Path(worktree.path))
    if worktree.git_dir is not None:
        return WorktreeLocation(work_tree=work_tree, git_dir=Path(worktree.git_dir))
    return WorktreeLocation(
        work_tree=work_tree,
        git_dir=await git.worktree_admin_dir(repo_path, work_tree),
    )


def setup_runner(settings: Settings, srt_path: Path) -> SrtCommandRunner:
    """The command runner for a repository's owner-registered setup command.

    The setup command is allowed to reach exactly the package registries named
    in :attr:`Settings.setup_allowed_domains`; everything else stays denied by
    the sandbox.
    """
    return SrtCommandRunner(
        settings,
        srt_path=srt_path,
        allowed_domains=settings.setup_allowed_domains,
    )


def check_runner(settings: Settings, srt_path: Path) -> SrtCommandRunner:
    """The command runner for a step's check command: no network at all."""
    return SrtCommandRunner(settings, srt_path=srt_path, allowed_domains=())
