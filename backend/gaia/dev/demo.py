"""``gaia demo init``: a tiny local repository and ticket for a first run.

The demo is intentionally dependency-free: the repository contains one Python
function and one stdlib ``unittest`` test, so its registered test command runs
in the sandbox without installing anything or reaching the network.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession

from gaia.config.settings import Settings, ensure_data_dir
from gaia.db.engine import create_engine_for, create_session_factory, session_scope
from gaia.db.models import Repository, Ticket
from gaia.db.repo import ScopedRepo
from gaia.db.seed import seed_initial_data
from gaia.services.git import GitService

DEMO_REPO_NAME = "greet"
DEMO_TICKET_TITLE = "Add a farewell() function"
DEMO_TICKET_BODY = (
    "Add a `farewell(name)` function next to `greet(name)` in `greet.py`.\n\n"
    'The function should return `f"Goodbye, {name}!"`. Add a stdlib '
    "`unittest` test in `test_greet.py` that covers the new function."
)
DEMO_TEST_COMMAND = ["python3", "-m", "unittest", "-q"]

GREET_PY = '''"""A tiny demo module for Gaia."""


def greet(name: str) -> str:
    """Return a friendly greeting."""
    return f"Hello, {name}!"
'''

TEST_GREET_PY = '''"""Stdlib-only tests for the Gaia demo repository."""

import unittest

from greet import greet


class GreetTests(unittest.TestCase):
    def test_greet(self) -> None:
        self.assertEqual(greet("Ada"), "Hello, Ada!")


if __name__ == "__main__":
    unittest.main()
'''


@dataclass(frozen=True)
class DemoInitResult:
    """What ``gaia demo init`` created or reused."""

    repo_path: Path
    repository_id: str
    ticket_id: str
    created_repo: bool
    created_registration: bool
    created_ticket: bool


async def init_demo(settings: Settings) -> int:
    """Create the demo repository, registration and ticket; print the next step."""
    ensure_data_dir(settings)
    git = GitService(settings)
    engine = create_engine_for(settings)
    sessions = create_session_factory(engine)
    try:
        repo_path = settings.data_dir / "demo" / DEMO_REPO_NAME
        created_repo = await _ensure_demo_repo(git, repo_path)

        async with session_scope(sessions) as session:
            seeded = await seed_initial_data(session, settings)
            workspace_id = seeded.workspace.id
            user_id = seeded.user.id
            repository, created_registration = await _ensure_repository(
                session,
                workspace_id=workspace_id,
                user_id=user_id,
                repo_path=repo_path,
                git=git,
            )
            ticket, created_ticket = await _ensure_ticket(
                session,
                workspace_id=workspace_id,
                user_id=user_id,
                repository=repository,
            )

        print(f"demo repository: {repo_path}")
        print(f"repository id: {repository.id}")
        print(f"ticket id: {ticket.id}")
        if created_repo or created_registration or created_ticket:
            print("demo initialized")
        else:
            print("demo already initialized; nothing duplicated")
        print(
            "Next step: start `GAIA_FAKE_LLM=1 gaia serve`, open the UI, and "
            "press Start run on the ticket."
        )
        return 0
    finally:
        await engine.dispose()


def _prepare_demo_repo(repo_path: Path) -> bool:
    """Create the repo directory and write the two demo files; return whether new."""
    repo_path.mkdir(parents=True, exist_ok=True)
    created = not (repo_path / ".git").exists()
    (repo_path / "greet.py").write_text(GREET_PY, encoding="utf-8")
    (repo_path / "test_greet.py").write_text(TEST_GREET_PY, encoding="utf-8")
    return created


async def _ensure_demo_repo(git: GitService, repo_path: Path) -> bool:
    """Create the repo and its one commit on ``main``; reuse an existing repo."""
    created = _prepare_demo_repo(repo_path)
    if created:
        await git._run("init", "--initial-branch=main", cwd=repo_path)

    if await git.status_porcelain(repo_path):
        await git.commit(repo_path, "demo: add greet() and its test")
    return created


async def _ensure_repository(
    session: AsyncSession,
    *,
    workspace_id: str,
    user_id: str,
    repo_path: Path,
    git: GitService,
) -> tuple[Repository, bool]:
    """Register the demo repo exactly as ``POST /repositories`` does."""
    repositories = ScopedRepo(session, Repository, workspace_id)
    existing = await repositories.list(name=repo_path.name) or await repositories.list(
        local_path=str(repo_path)
    )
    if existing:
        return existing[0], False

    validated = await git.validate_repo(repo_path)
    remote_url = await git.remote_url(validated)
    default_branch = await git.default_branch(validated)
    repository = await repositories.create(
        name=validated.name,
        local_path=str(validated),
        remote_url=remote_url,
        default_branch=default_branch,
        setup_command=None,
        test_command=DEMO_TEST_COMMAND,
        created_by=user_id,
    )
    return repository, True


async def _ensure_ticket(
    session: AsyncSession,
    *,
    workspace_id: str,
    user_id: str,
    repository: Repository,
) -> tuple[Ticket, bool]:
    """Open the demo ticket once; never duplicate it."""
    tickets = ScopedRepo(session, Ticket, workspace_id)
    existing = await tickets.list(repo_id=repository.id, title=DEMO_TICKET_TITLE)
    if existing:
        return existing[0], False
    ticket = await tickets.create(
        repo_id=repository.id,
        title=DEMO_TICKET_TITLE,
        body=DEMO_TICKET_BODY,
        created_by=user_id,
    )
    return ticket, True
