"""Run-token minting, resolution and revocation (doc 07b)."""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from gaia.db.models import Run, RunToken
from gaia.db.repo import RunTokenRepo, ScopedRepo
from gaia.db.types import utcnow
from gaia.domain.enums import RunStatus
from gaia.llm.run_tokens import (
    RUN_TOKEN_PREFIX,
    RunTokenContext,
    mint,
    resolve,
    revoke,
    revoke_for_run,
    token_hash,
)
from tests.db_builders import make_chain


async def test_mint_returns_a_token_once_and_stores_only_its_hash(session: AsyncSession) -> None:
    chain = await make_chain(session, slug="mint")
    minted = await mint(
        session,
        workspace_id=chain.workspace.id,
        run_id=chain.run.id,
        provider="deepseek",
        ttl_s=60,
        run_step_id=chain.step.id,
        agent_id=chain.agent.id,
    )

    plaintext = minted.token.get_secret_value()
    assert plaintext.startswith(RUN_TOKEN_PREFIX)
    assert minted.token_id
    assert minted.expires_at > utcnow()

    rows = await ScopedRepo(session, RunToken, chain.workspace.id).list()
    assert len(rows) == 1
    row = rows[0]
    assert row.token_hash == token_hash(plaintext)
    assert plaintext not in row.token_hash
    assert row.run_id == chain.run.id
    assert row.run_step_id == chain.step.id
    assert row.agent_id == chain.agent.id
    assert row.provider == "deepseek"
    assert row.revoked_at is None


@pytest.mark.parametrize("provider", ["", "openai"])
async def test_mint_rejects_an_unknown_provider(session: AsyncSession, provider: str) -> None:
    chain = await make_chain(session, slug=f"provider-{provider or 'empty'}")
    with pytest.raises(ValueError, match="unknown LLM provider"):
        await mint(
            session,
            workspace_id=chain.workspace.id,
            run_id=chain.run.id,
            provider=provider,
            ttl_s=60,
        )


async def test_mint_rejects_a_non_positive_ttl(session: AsyncSession) -> None:
    chain = await make_chain(session, slug="ttl")
    with pytest.raises(ValueError, match="ttl_s"):
        await mint(
            session,
            workspace_id=chain.workspace.id,
            run_id=chain.run.id,
            provider="deepseek",
            ttl_s=0,
        )


async def test_resolve_returns_context_for_a_live_token(session: AsyncSession) -> None:
    chain = await make_chain(session, slug="resolve")
    minted = await mint(
        session,
        workspace_id=chain.workspace.id,
        run_id=chain.run.id,
        provider="anthropic",
        ttl_s=60,
        run_step_id=chain.step.id,
        agent_id=chain.agent.id,
    )
    plaintext = minted.token.get_secret_value()

    context = await resolve(session, plaintext)

    assert context == RunTokenContext(
        token_id=minted.token_id,
        workspace_id=chain.workspace.id,
        run_id=chain.run.id,
        run_step_id=chain.step.id,
        agent_id=chain.agent.id,
        provider="anthropic",
    )


async def test_resolve_rejects_missing_unknown_and_None(session: AsyncSession) -> None:
    chain = await make_chain(session, slug="resolve-none")
    assert await resolve(session, None) is None
    assert await resolve(session, "gaia_rt_not-a-real-token") is None
    assert await resolve(session, "") is None
    assert chain.run.id  # keep the chain used


async def test_resolve_rejects_a_revoked_token(session: AsyncSession) -> None:
    chain = await make_chain(session, slug="resolve-revoked")
    minted = await mint(
        session, workspace_id=chain.workspace.id, run_id=chain.run.id, provider="deepseek", ttl_s=60
    )
    await revoke(session, chain.workspace.id, minted.token_id)

    assert await resolve(session, minted.token.get_secret_value()) is None


async def test_resolve_rejects_an_expired_token(session: AsyncSession) -> None:
    chain = await make_chain(session, slug="resolve-expired")
    minted = await mint(
        session, workspace_id=chain.workspace.id, run_id=chain.run.id, provider="deepseek", ttl_s=60
    )
    tokens = ScopedRepo(session, RunToken, chain.workspace.id)
    await tokens.update(minted.token_id, expires_at=utcnow() - timedelta(seconds=1))

    assert await resolve(session, minted.token.get_secret_value()) is None


@pytest.mark.parametrize(
    "status", [RunStatus.COMPLETED.value, RunStatus.FAILED.value, RunStatus.CANCELLED.value]
)
async def test_resolve_rejects_a_terminal_run(session: AsyncSession, status: str) -> None:
    chain = await make_chain(session, slug=f"resolve-terminal-{status}")
    runs = ScopedRepo(session, Run, chain.workspace.id)
    await runs.update(chain.run.id, status=status)
    minted = await mint(
        session, workspace_id=chain.workspace.id, run_id=chain.run.id, provider="deepseek", ttl_s=60
    )

    assert await resolve(session, minted.token.get_secret_value()) is None


async def test_resolve_rejects_a_missing_run(session: AsyncSession) -> None:
    chain = await make_chain(session, slug="resolve-missing-run")
    minted = await mint(
        session, workspace_id=chain.workspace.id, run_id=chain.run.id, provider="deepseek", ttl_s=60
    )
    runs = ScopedRepo(session, Run, chain.workspace.id)
    await runs.delete(chain.run.id)

    assert await resolve(session, minted.token.get_secret_value()) is None


async def test_revoke_is_idempotent(session: AsyncSession) -> None:
    chain = await make_chain(session, slug="revoke-idempotent")
    minted = await mint(
        session, workspace_id=chain.workspace.id, run_id=chain.run.id, provider="deepseek", ttl_s=60
    )

    await revoke(session, chain.workspace.id, minted.token_id)
    first = await RunTokenRepo(session).get(minted.token_id)
    assert first is not None
    assert first.revoked_at is not None
    await revoke(session, chain.workspace.id, minted.token_id)
    second = await RunTokenRepo(session).get(minted.token_id)
    assert second is not None
    assert second.revoked_at == first.revoked_at


async def test_revoke_for_run_is_scoped_and_idempotent(session: AsyncSession) -> None:
    chain = await make_chain(session, slug="revoke-run")
    other = await make_chain(session, slug="revoke-other")
    minted = await mint(
        session, workspace_id=chain.workspace.id, run_id=chain.run.id, provider="deepseek", ttl_s=60
    )
    other_minted = await mint(
        session, workspace_id=other.workspace.id, run_id=other.run.id, provider="deepseek", ttl_s=60
    )

    await revoke_for_run(session, chain.workspace.id, chain.run.id)
    assert await resolve(session, minted.token.get_secret_value()) is None
    assert await resolve(session, other_minted.token.get_secret_value()) is not None

    first = await RunTokenRepo(session).get(minted.token_id)
    assert first is not None
    assert first.revoked_at is not None
    await revoke_for_run(session, chain.workspace.id, chain.run.id)
    second = await RunTokenRepo(session).get(minted.token_id)
    assert second is not None
    assert second.revoked_at == first.revoked_at


async def test_a_minted_token_is_masked_by_shape_without_registration(
    session: AsyncSession,
) -> None:
    """Lead review 07b (G3): masked by the gaia_rt_ pattern; the registry stays empty."""
    from gaia.security import redaction

    chain = await make_chain(session, slug="token-redaction")
    minted = await mint(
        session, workspace_id=chain.workspace.id, run_id=chain.run.id, provider="deepseek", ttl_s=60
    )
    plaintext = minted.token.get_secret_value()

    assert redaction.redact(f"env ANTHROPIC_API_KEY={plaintext}") == (
        f"env ANTHROPIC_API_KEY={redaction.REDACTED}"
    )
    assert plaintext not in redaction._registered


async def test_revoke_ignores_a_token_from_another_workspace(session: AsyncSession) -> None:
    """Lead review 07b (G4): revocation is workspace-scoped, like every other write."""
    chain = await make_chain(session, slug="revoke-scope")
    other = await make_chain(session, slug="revoke-scope-other")
    minted = await mint(
        session, workspace_id=chain.workspace.id, run_id=chain.run.id, provider="deepseek", ttl_s=60
    )

    await revoke(session, other.workspace.id, minted.token_id)

    assert await resolve(session, minted.token.get_secret_value()) is not None
