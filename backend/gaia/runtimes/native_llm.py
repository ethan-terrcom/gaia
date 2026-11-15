"""Provider-neutral LLM handle construction for the native runtime (doc 10b).

The native loop never builds a vendor client. This module owns the one place a
``gaia dev run-task --runtime native`` process turns a per-task run token into a
:class:`~gaia.llm.deepseek_client.DeepSeekClient` pointed at the gateway. The
gateway remains the only writer of ``llm_usage`` rows (ADR-001 P4), so the
client's recorder is an in-memory :class:`UsageTally`, never a database writer.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Literal

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from gaia.config.settings import Settings
from gaia.db.engine import session_scope
from gaia.llm import run_tokens
from gaia.llm.base import LLMUsage, ToolChatLLM
from gaia.llm.deepseek_client import DeepSeekClient
from gaia.runtimes.base import AgentTask, RuntimeUsage
from gaia.services.costs import PriceTable


class UsageTally:
    """An in-memory :class:`~gaia.llm.base.UsageRecorder` for one task.

    The gateway has already written the durable ``llm_usage`` row; this tally
    only sums the numbers so the runtime can return a :class:`RuntimeUsage`.
    """

    def __init__(self) -> None:
        self._input_tokens = 0
        self._output_tokens = 0
        self._cache_tokens = 0
        self._cost_usd = Decimal(0)

    async def record(
        self,
        usage: LLMUsage,
        *,
        cost_usd: Decimal,
        price_tier: str | None = None,
    ) -> None:
        """Accumulate one recorded call. ``price_tier`` is deliberately unused."""
        del price_tier
        self._input_tokens += usage.input_tokens
        self._output_tokens += usage.output_tokens
        self._cache_tokens += usage.cache_read_tokens + usage.cache_creation_tokens
        self._cost_usd += cost_usd

    def total(self) -> RuntimeUsage:
        """Return the task's token and cost totals so far."""
        return RuntimeUsage(
            input_tokens=self._input_tokens,
            output_tokens=self._output_tokens,
            cache_tokens=self._cache_tokens,
            cost_usd=self._cost_usd,
        )


@dataclass(frozen=True)
class LlmHandle:
    """A per-task model handle and its cleanup callbacks."""

    llm: ToolChatLLM
    usage: UsageTally
    close: Callable[[], Awaitable[None]]


def gateway_llm_factory(
    *,
    settings: Settings,
    sessions: async_sessionmaker[AsyncSession],
    price_table: PriceTable,
    model: str,
    provider: Literal["deepseek"] = "deepseek",
    ttl_s: int = 7200,
    gateway_base: str | None = None,
    # Test seam: the in-process gateway test supplies an ASGI transport for the
    # real OpenAI client. Production leaves this ``None``.
    http_client: Any | None = None,
) -> Callable[[AgentTask], Awaitable[LlmHandle]]:
    """Build the native runtime's ``llm_factory``.

    Each task gets one run token, minted in its own transaction and revoked when
    the returned handle is closed. The client's only credential is that token:
    no provider key ever enters this module's runtime path.
    """

    async def factory(task: AgentTask) -> LlmHandle:
        async with session_scope(sessions) as session:
            minted = await run_tokens.mint(
                session,
                workspace_id=task.workspace_id,
                run_id=task.run_id,
                run_step_id=task.run_step_id,
                agent_id=task.agent_id,
                provider=provider,
                ttl_s=ttl_s,
            )
        token_id = minted.token_id
        workspace_id = task.workspace_id
        # ``gateway_base`` is the server origin, like the default; the provider
        # route is always appended (lead review N3).
        origin = (gateway_base or f"http://{settings.host}:{settings.port}").rstrip("/")
        base_url = f"{origin}/llm/deepseek"
        tally = UsageTally()
        client = DeepSeekClient(
            api_key=minted.token,
            model=model,
            base_url=base_url,
            recorder=tally,
            price_table=price_table,
            http_client=http_client,
        )

        async def close() -> None:
            # Revoke even if closing the client fails: a live token outlasting
            # its task is the worse outcome.
            try:
                await client.aclose()
            finally:
                async with session_scope(sessions) as session:
                    await run_tokens.revoke(session, workspace_id, token_id)

        return LlmHandle(llm=client, usage=tally, close=close)

    return factory
