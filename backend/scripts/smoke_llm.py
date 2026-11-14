"""One tiny real call to each LLM provider, for the owner to run by hand.

This is intentionally **not** part of the test suite: it uses the real keychain
and the real provider APIs. Run it with ``make smoke-llm``.
"""

from __future__ import annotations

import asyncio
from decimal import Decimal

from pydantic import SecretStr

from gaia.config.secrets import SecretName, SecretStore, get_secret_store
from gaia.config.settings import get_settings
from gaia.llm.anthropic_client import AnthropicClient
from gaia.llm.base import LLMMessage, LLMUsage, UsageRecorder
from gaia.llm.deepseek_client import DeepSeekClient
from gaia.services.costs import load_price_table


class _PrintRecorder:
    """Print instead of writing to the database; a smoke run has no run row."""

    async def record(
        self,
        usage: LLMUsage,
        *,
        cost_usd: Decimal,
        price_tier: str | None = None,
    ) -> None:
        print(
            f"recorded provider={usage.provider} model={usage.model} "
            f"input={usage.input_tokens} output={usage.output_tokens} "
            f"cache_read={usage.cache_read_tokens} cache_write={usage.cache_creation_tokens} "
            f"tier={price_tier} cost_usd={cost_usd}"
        )


def _secret(store: SecretStore, name: SecretName) -> SecretStr:
    return store.require(name)


#: Opus 5.5 always thinks, and thinking counts against max_tokens, so a tiny
#: cap would end in LLMStop("max_tokens") instead of a reply.
SMOKE_MAX_TOKENS = 2048


async def _main() -> None:
    settings = get_settings()
    store = get_secret_store()
    price_table = load_price_table()
    recorder: UsageRecorder = _PrintRecorder()

    anthropic = AnthropicClient(
        api_key=_secret(store, SecretName.ANTHROPIC_API_KEY),
        model=settings.lead_model,
        recorder=recorder,
        price_table=price_table,
    )
    response = await anthropic.complete(
        system="You are a smoke test.",
        messages=[LLMMessage(role="user", content="Reply with the single word pong.")],
        max_tokens=SMOKE_MAX_TOKENS,
    )
    print(
        f"anthropic ok model={response.model} "
        f"request_id={response.request_id} text={response.text!r}"
    )

    deepseek = DeepSeekClient(
        api_key=_secret(store, SecretName.DEEPSEEK_API_KEY),
        model=settings.junior_model,
        base_url=settings.junior_base_url,
        recorder=recorder,
        price_table=price_table,
    )
    response = await deepseek.complete(
        messages=[LLMMessage(role="user", content="Reply with the single word pong.")],
        max_tokens=SMOKE_MAX_TOKENS,
    )
    print(
        f"deepseek ok model={response.model} "
        f"request_id={response.request_id} text={response.text!r}"
    )


def main() -> None:
    """Run the smoke test."""
    asyncio.run(_main())


if __name__ == "__main__":
    main()
