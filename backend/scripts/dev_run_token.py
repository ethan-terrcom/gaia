"""Mint one gateway run token for a scratch repository, ticket and run.

This is a developer helper for the 07b acceptance criterion: the lead can run
``uv run python scripts/dev_run_token.py --provider deepseek`` and point a
harness at the printed token and run id. It seeds a scratch repository/ticket/
run in the configured ``GAIA_DATA_DIR`` database, mints a token, and prints
exactly the plaintext token and the run id - never a provider key.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
from decimal import Decimal
from pathlib import Path

from gaia.config.logging import configure_logging
from gaia.config.settings import Settings, ensure_data_dir
from gaia.db.engine import create_engine_for, create_session_factory, session_scope
from gaia.db.ids import uuid7
from gaia.db.models import Repository, Run, Ticket
from gaia.db.repo import ScopedRepo
from gaia.db.seed import seed_initial_data
from gaia.domain.enums import RuntimeKind
from gaia.llm.run_tokens import mint
from gaia.services.git import GitService


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--provider", choices=("anthropic", "deepseek"), required=True)
    parser.add_argument("--ttl", type=int, default=3600)
    parser.add_argument(
        "--repo-path",
        type=Path,
        default=Path.cwd(),
        help="repository path to register (default: the current directory)",
    )
    return parser


async def _main() -> None:
    args = _parser().parse_args()
    settings = Settings()
    ensure_data_dir(settings)
    # The script's contract is "print only the token and run id". GitService and
    # the database emit debug logs through structlog; silence those by default.
    configure_logging(level=logging.WARNING)
    git = GitService(settings)
    repo_path = await git.validate_repo(args.repo_path)

    engine = create_engine_for(settings)
    sessions = create_session_factory(engine)
    try:
        async with session_scope(sessions) as session:
            seeded = await seed_initial_data(session, settings)
            repositories = ScopedRepo(session, Repository, seeded.workspace.id)
            repository = await repositories.create(
                name=f"scratch-{uuid7()[:8]}",
                local_path=str(repo_path),
                remote_url=None,
                setup_command=None,
                test_command=None,
                created_by=seeded.user.id,
            )
            tickets = ScopedRepo(session, Ticket, seeded.workspace.id)
            ticket = await tickets.create(
                repo_id=repository.id,
                title="Scratch gateway run",
                body="Created by scripts/dev_run_token.py for a live gateway check.",
                created_by=seeded.user.id,
            )
            runs = ScopedRepo(session, Run, seeded.workspace.id)
            run = await runs.create(
                ticket_id=ticket.id,
                repo_id=repository.id,
                worktree_id=None,
                junior_runtime=(
                    RuntimeKind.NATIVE_DEEPSEEK.value
                    if args.provider == "deepseek"
                    else RuntimeKind.NATIVE_CLAUDE.value
                ),
                graph_thread_id=None,
                budget_usd=Decimal(str(settings.run_budget_usd)),
                created_by=seeded.user.id,
            )
            minted = await mint(
                session,
                workspace_id=seeded.workspace.id,
                run_id=run.id,
                provider=args.provider,
                ttl_s=args.ttl,
            )
            token = minted.token.get_secret_value()
            run_id = run.id

        print(token)
        print(run_id)
    finally:
        await engine.dispose()


def main() -> None:
    """Run the helper."""
    asyncio.run(_main())


if __name__ == "__main__":
    main()
