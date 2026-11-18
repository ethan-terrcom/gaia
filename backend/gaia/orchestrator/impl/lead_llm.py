"""Per-call LLM access for the lead's planner and reviewer (12b-2).

Each planning or review call mints one short-lived run token in its own
transaction, builds a provider client pointed at Gaia's gateway, and yields it
inside an async context manager. When the call is done the token is revoked and
the client is closed, even if the model call raised.

The gateway is the only writer of ``llm_usage`` rows (ADR-001 P4), so the
client's recorder is 10b's in-memory :class:`UsageTally`, never a database
writer.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from gaia.config.settings import Settings
from gaia.db.engine import session_scope
from gaia.llm import run_tokens
from gaia.llm.base import StructuredLLM
from gaia.llm.providers import build_structured_llm
from gaia.runtimes.native_llm import UsageTally
from gaia.services.costs import PriceTable


@asynccontextmanager
async def lead_llm(
    *,
    settings: Settings,
    sessions: async_sessionmaker[AsyncSession],
    price_table: PriceTable,
    run_id: str,
    workspace_id: str,
    model: str | None = None,
    http_client: Any | None = None,
) -> AsyncIterator[StructuredLLM]:
    """Yield a structured LLM for one lead call, revoking its token afterwards.

    ``model`` is the run's lead (or panelist) model from its agent config
    (doc 19b); ``None`` means ``settings.planner_model``. Its provider comes
    from the model catalog, falling back to ``settings.planner_provider`` for a
    model the price table does not assign.

    ``http_client`` is the same test seam as in
    :func:`gaia.runtimes.native_llm.gateway_llm_factory`: production passes
    ``None``, and the in-process gateway tests pass an ASGI transport.
    """
    chosen_model = model or settings.planner_model
    provider = price_table.provider_of(chosen_model) or settings.planner_provider
    async with session_scope(sessions) as session:
        minted = await run_tokens.mint(
            session,
            workspace_id=workspace_id,
            run_id=run_id,
            provider=provider,
            ttl_s=int(settings.lead_call_timeout_s) + 300,
            run_step_id=None,
            agent_id=None,
        )
    token_id = minted.token_id
    origin = f"http://{settings.host}:{settings.port}"
    base_url = f"{origin.rstrip('/')}/llm/{provider}"
    # Omit ``http_client`` when it is ``None`` so monkeypatched factories in
    # tests keep their existing signatures; both branches build the same client
    # in production.
    if http_client is None:
        client = build_structured_llm(
            provider=provider,
            model=chosen_model,
            api_key=minted.token,
            base_url=base_url,
            recorder=UsageTally(),
            price_table=price_table,
        )
    else:
        client = build_structured_llm(
            provider=provider,
            model=chosen_model,
            api_key=minted.token,
            base_url=base_url,
            recorder=UsageTally(),
            price_table=price_table,
            http_client=http_client,
        )
    try:
        yield client
    finally:
        # Revoke even if closing the client fails: a live token outlasting its
        # lead call is the worse outcome (same order as native_llm's close).
        try:
            close = getattr(client, "aclose", None)
            if close is not None:
                await close()
        finally:
            async with session_scope(sessions) as session:
                await run_tokens.revoke(session, workspace_id, token_id)
