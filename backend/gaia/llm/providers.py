"""Provider-neutral structured-LLM construction (ADR-001 P2).

The orchestrator resolves *which* provider and model the lead uses from
settings, not from a vendor import. This module is the only place outside the
two adapter files that needs to know which class goes with which provider name;
it imports the adapters, never ``anthropic`` or ``openai`` themselves (P1).
"""

from __future__ import annotations

from typing import Literal

from pydantic import SecretStr

from gaia.llm.anthropic_client import AnthropicClient
from gaia.llm.base import StructuredLLM, UsageRecorder
from gaia.llm.deepseek_client import DeepSeekClient
from gaia.services.costs import PriceTable


def build_structured_llm(
    *,
    provider: Literal["anthropic", "deepseek"],
    model: str,
    api_key: SecretStr,
    base_url: str,
    recorder: UsageRecorder,
    price_table: PriceTable,
) -> StructuredLLM:
    """Build the lead's structured-output client for one configured provider."""
    if provider == "anthropic":
        return AnthropicClient(
            api_key=api_key,
            model=model,
            base_url=base_url,
            recorder=recorder,
            price_table=price_table,
        )
    return DeepSeekClient(
        api_key=api_key,
        model=model,
        base_url=base_url,
        recorder=recorder,
        price_table=price_table,
    )
