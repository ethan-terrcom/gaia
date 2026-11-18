"""The model catalog: which models Gaia can use, and from which provider (19a).

The catalog is not a second registry. It is a read of ``prices.toml``: a model
with a ``provider`` there can be picked for a role, and the gateway will meter
and forward it. Whether it can actually *run* depends on the provider's key
being in the keychain, which :func:`provider_configured` answers.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from gaia.config.secrets import SecretStore, provider_secret
from gaia.domain.enums import LLMProvider, ModelTier
from gaia.services.costs import PriceTable

#: Display order: strongest first inside each provider.
_TIER_ORDER = {ModelTier.FRONTIER.value: 0, ModelTier.MID.value: 1, ModelTier.FAST.value: 2}
_PROVIDER_ORDER = {provider.value: index for index, provider in enumerate(LLMProvider)}


@dataclass(frozen=True)
class CatalogModel:
    """One model Gaia can put in a role."""

    id: str
    provider: str
    label: str
    tier: str | None
    input_usd_per_mtok: Decimal
    output_usd_per_mtok: Decimal
    cache_read_usd_per_mtok: Decimal


def catalog_models(prices: PriceTable) -> list[CatalogModel]:
    """Return every priced model that names its provider, in display order."""
    models: list[CatalogModel] = []
    for model_id in prices.model_names():
        price = prices.get(model_id)
        if price is None or price.provider is None:
            continue
        models.append(
            CatalogModel(
                id=model_id,
                provider=price.provider,
                label=price.label or model_id,
                tier=price.tier,
                input_usd_per_mtok=price.rates.input,
                output_usd_per_mtok=price.rates.output,
                cache_read_usd_per_mtok=price.rates.cache_read,
            )
        )
    models.sort(
        key=lambda model: (
            _PROVIDER_ORDER[model.provider],
            _TIER_ORDER.get(model.tier or "", len(_TIER_ORDER)),
            model.label,
        )
    )
    return models


def provider_configured(store: SecretStore, provider: str, *, fake_llm: bool = False) -> bool:
    """Return whether ``provider``'s key is in the store (always true in demo mode).

    Only presence is checked; the value is never returned or compared. Demo mode
    answers every provider with the local fake, so no key is needed there.
    """
    if fake_llm:
        return True
    return store.get(provider_secret(provider)) is not None
