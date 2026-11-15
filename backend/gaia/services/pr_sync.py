"""Background sync of Gaia's open pull requests with GitHub (doc 15b).

Humans can close, merge, review or push to a PR directly on GitHub. The API
server runs :class:`PRSyncService` in the background so Gaia's ``pull_request``
rows and ``pr.updated`` events catch up without a webhook.

The service is deliberately resilient per PR: one broken repository or PR is
logged and skipped, and a rate limit stops only the current round.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

import httpx
import structlog
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from gaia.config.secrets import SecretName, SecretStore
from gaia.config.settings import Settings
from gaia.db.engine import session_scope
from gaia.db.models import PullRequest
from gaia.db.repo import PullRequestRepo, ScopedRepo
from gaia.domain.enums import EventType
from gaia.services.ci import ci_rollup, review_rollup
from gaia.services.events import EventBus
from gaia.services.github import (
    GitHubClient,
    GitHubError,
    RateLimited,
    RepoSlug,
    UnsupportedRemote,
    parse_github_remote,
)

logger = structlog.get_logger(__name__)


class PRSyncService:
    """Poll every open PR across every workspace and refresh Gaia's copy."""

    def __init__(
        self,
        *,
        sessions: async_sessionmaker[AsyncSession],
        secrets: SecretStore,
        bus: EventBus,
        settings: Settings,
        client_factory: Callable[[SecretStr], GitHubClient] = GitHubClient,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.sessions = sessions
        self.secrets = secrets
        self.bus = bus
        self.settings = settings
        self.client_factory = client_factory
        self._sleep = sleep

    async def sync_once(self) -> int:
        """Refresh every open PR once and return how many rows changed."""
        token = self.secrets.get(SecretName.GITHUB_TOKEN)
        if token is None:
            logger.debug("pr_sync.no_token")
            return 0

        async with self.sessions() as session:
            rows = await PullRequestRepo(session).list_open()

        changed = 0
        async with self.client_factory(token) as client:
            for row in rows:
                pr = row.pull_request
                slug = _parse_slug(row.repository.remote_url)
                if slug is None:
                    logger.debug(
                        "pr_sync.skip_remote",
                        run_id=pr.run_id,
                        number=pr.number,
                    )
                    continue

                try:
                    info = await client.get_pr(slug, pr.number)
                    reviews = await client.list_reviews(slug, pr.number)
                    combined = await client.get_combined_status(slug, info.head_sha)
                    checks = await client.list_check_runs(slug, info.head_sha)
                except RateLimited:
                    logger.warning("pr_sync.rate_limited")
                    break
                except GitHubError:
                    logger.warning(
                        "pr_sync.pr_failed",
                        run_id=pr.run_id,
                        number=pr.number,
                        exc_info=True,
                    )
                    continue

                review_state = review_rollup(reviews)
                ci_status = ci_rollup(combined, checks)
                if (
                    pr.state == info.state
                    and pr.ci_status == ci_status.value
                    and pr.review_state == review_state
                    and pr.head_sha == info.head_sha
                ):
                    continue

                async with session_scope(self.sessions) as session:
                    await ScopedRepo(session, PullRequest, pr.workspace_id).update(
                        pr.id,
                        state=info.state,
                        ci_status=ci_status.value,
                        review_state=review_state,
                        head_sha=info.head_sha,
                    )
                changed += 1
                await self.bus.publish(
                    workspace_id=pr.workspace_id,
                    type=EventType.PR_UPDATED,
                    payload={
                        "run_id": pr.run_id,
                        "number": pr.number,
                        "state": info.state,
                        "ci_status": ci_status.value,
                        "review_state": review_state,
                    },
                    run_id=pr.run_id,
                )
        return changed

    async def run_forever(self) -> None:
        """Run :meth:`sync_once` on an interval until cancelled.

        An unexpected exception is logged and the loop continues after the
        interval. :class:`asyncio.CancelledError` propagates, which is how the
        API lifespan stops this task.
        """
        while True:
            try:
                await self.sync_once()
            except asyncio.CancelledError:
                raise
            except httpx.TransportError as exc:
                # Offline (laptop asleep, no Wi-Fi) is normal for a local app:
                # one short warning per round, not a traceback every minute.
                logger.warning("pr_sync.offline", error=type(exc).__name__)
            except Exception:
                logger.exception("pr_sync.round_failed")
            await self._sleep(self.settings.pr_sync_interval_s)


def _parse_slug(remote_url: str | None) -> RepoSlug | None:
    """Return the GitHub slug for ``remote_url``, or ``None`` when it is absent/invalid."""
    if not remote_url:
        return None
    try:
        return parse_github_remote(remote_url)
    except UnsupportedRemote:
        return None
