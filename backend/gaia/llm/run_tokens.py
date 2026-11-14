"""Mint, resolve and revoke the per-run tokens used by the LLM gateway.

Doc 07b's threat model is a harness process that holds a provider key in its
environment. A run token is worthless outside Gaia: it is random, scoped to one
run, stored only as a SHA-256 hash, and checked on every gateway call. The
plaintext is returned once at mint time and never stored.
"""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta

from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncSession

from gaia.db.models import Run, RunToken
from gaia.db.repo import RunTokenRepo, ScopedRepo
from gaia.db.types import utcnow
from gaia.domain.enums import RunStatus

#: Plaintext prefix, so a token is recognisable without revealing anything.
RUN_TOKEN_PREFIX = "gaia_rt_"  # noqa: S105 - a public prefix, not a secret value
#: Entropy in the random half; ``token_urlsafe`` renders 32 bytes as 43 chars.
RUN_TOKEN_BYTES = 32

#: Providers the gateway can route to.
_PROVIDERS = frozenset({"anthropic", "deepseek"})

#: A run in one of these statuses can no longer make LLM calls.
_TERMINAL_RUN_STATUSES = frozenset(
    {RunStatus.COMPLETED.value, RunStatus.FAILED.value, RunStatus.CANCELLED.value}
)


@dataclass(frozen=True)
class MintedToken:
    """The one time a run-token plaintext exists in a variable."""

    token: SecretStr
    token_id: str
    expires_at: datetime


@dataclass(frozen=True)
class RunTokenContext:
    """The call context a resolved token proves."""

    token_id: str
    workspace_id: str
    run_id: str
    run_step_id: str | None
    agent_id: str | None
    provider: str


def token_hash(plaintext: str) -> str:
    """Return the stored SHA-256 hex digest for ``plaintext``."""
    return hashlib.sha256(plaintext.encode("utf-8")).hexdigest()


async def mint(
    session: AsyncSession,
    *,
    workspace_id: str,
    run_id: str,
    provider: str,
    ttl_s: int,
    run_step_id: str | None = None,
    agent_id: str | None = None,
) -> MintedToken:
    """Create one run token and return its plaintext exactly once.

    ``ttl_s`` must be positive. Only the SHA-256 digest is written to the
    database. The plaintext is masked in logs and events by the ``gaia_rt_``
    pattern in :mod:`gaia.security.redaction`, not by registration.
    """
    if provider not in _PROVIDERS:
        raise ValueError(f"unknown LLM provider {provider!r}")
    if ttl_s <= 0:
        raise ValueError("ttl_s must be positive")

    plaintext = RUN_TOKEN_PREFIX + secrets.token_urlsafe(RUN_TOKEN_BYTES)
    expires_at = utcnow() + timedelta(seconds=ttl_s)
    tokens = ScopedRepo(session, RunToken, workspace_id)
    row = await tokens.create(
        run_id=run_id,
        run_step_id=run_step_id,
        agent_id=agent_id,
        provider=provider,
        token_hash=token_hash(plaintext),
        expires_at=expires_at,
    )
    return MintedToken(
        token=SecretStr(plaintext),
        token_id=row.id,
        expires_at=row.expires_at,
    )


async def resolve(session: AsyncSession, presented: str | None) -> RunTokenContext | None:
    """Return the context for ``presented``, or ``None`` when it is not usable.

    ``None`` covers every failure mode: missing token, unknown hash, revoked,
    expired, run missing, or run in a terminal status.
    """
    if presented is None:
        return None
    token = await RunTokenRepo(session).get_by_hash(token_hash(presented))
    if token is None:
        return None
    now = utcnow()
    if token.revoked_at is not None or token.expires_at <= now:
        return None

    run = await ScopedRepo(session, Run, token.workspace_id).get(token.run_id)
    if run is None or run.status in _TERMINAL_RUN_STATUSES:
        return None

    return RunTokenContext(
        token_id=token.id,
        workspace_id=token.workspace_id,
        run_id=token.run_id,
        run_step_id=token.run_step_id,
        agent_id=token.agent_id,
        provider=token.provider,
    )


async def revoke(session: AsyncSession, workspace_id: str, token_id: str) -> None:
    """Revoke one token by id, within one workspace. Revoking twice is a no-op."""
    await RunTokenRepo(session).revoke_by_id(workspace_id, token_id)


async def revoke_for_run(session: AsyncSession, workspace_id: str, run_id: str) -> None:
    """Revoke every live token for one run. Revoking twice is a no-op."""
    await RunTokenRepo(session).revoke_for_run(workspace_id, run_id)
