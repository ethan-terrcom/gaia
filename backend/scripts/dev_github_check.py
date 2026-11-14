"""Check the keychain GITHUB_TOKEN against a GitHub owner/repository.

This is the lead's no-network-needed-by-us helper for the doc 15a acceptance
check: it reads the token from the OS keychain, calls the same
``check_token`` used by ``PullRequestService``, and prints the permission table.

Usage:
    uv run python scripts/dev_github_check.py owner/name

The token is never printed.
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from gaia.config.logging import configure_logging
from gaia.config.secrets import SecretName, get_secret_store
from gaia.services.github import GitHubClient, RepoSlug, check_token


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("slug", help="GitHub owner/repository, for example acme/widgets")
    return parser


async def _main() -> None:
    args = _parser().parse_args()
    owner, separator, name = args.slug.partition("/")
    if not separator or not owner or not name or "/" in name:
        print(f"error: {args.slug!r} is not an owner/repository slug", file=sys.stderr)
        raise SystemExit(2)
    configure_logging()
    store = get_secret_store()
    token = store.require(SecretName.GITHUB_TOKEN)
    slug = RepoSlug(owner=owner, name=name)
    async with GitHubClient(token) as client:
        result = await check_token(client, slug)

    permissions = (
        "Metadata R",
        "Contents R/W",
        "Pull requests R",
        "Issues R",
        "Commit statuses R",
        "Checks R",
    )
    for permission in permissions:
        status = "ok" if permission not in result.missing else "missing"
        print(f"{permission:<20} {status}")
    if not result.ok:
        print(
            "GitHub token is missing permissions. See "
            "docs/implementation/15-github-integration.md for the required "
            "fine-grained PAT scopes.",
            file=sys.stderr,
        )
        raise SystemExit(1)


def main() -> None:
    """Run the helper."""
    asyncio.run(_main())


if __name__ == "__main__":
    main()
