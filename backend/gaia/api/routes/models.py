"""The model catalog API (19a): which models and providers Gaia can use."""

from __future__ import annotations

from fastapi import APIRouter
from starlette.concurrency import run_in_threadpool

from gaia.api.deps import ServicesDep
from gaia.config.secrets import provider_secret
from gaia.domain.enums import LLMProvider, ModelTier
from gaia.domain.schemas import CatalogModelRead, ModelCatalog, ProviderStatus
from gaia.llm.catalog import catalog_models, provider_configured

router = APIRouter(tags=["models"])


@router.get("/models", response_model=ModelCatalog, summary="List the models Gaia can use")
async def list_models(services: ServicesDep) -> ModelCatalog:
    """Return every provider with whether its key is set, and every priced model.

    A model is ``available`` when its provider's key is in the keychain (or in
    demo mode). Key values are never read into the response.
    """
    configured: dict[str, bool] = {}
    for provider in LLMProvider:
        # The keychain call can block; keep it off the event loop.
        configured[provider.value] = await run_in_threadpool(
            provider_configured,
            services.store,
            provider.value,
            fake_llm=services.settings.fake_llm,
        )
    return ModelCatalog(
        providers=[
            ProviderStatus(
                name=provider,
                configured=configured[provider.value],
                secret_name=provider_secret(provider.value).value,
            )
            for provider in LLMProvider
        ],
        models=[
            CatalogModelRead(
                id=model.id,
                provider=LLMProvider(model.provider),
                label=model.label,
                tier=ModelTier(model.tier) if model.tier is not None else None,
                available=configured[model.provider],
                input_usd_per_mtok=model.input_usd_per_mtok,
                output_usd_per_mtok=model.output_usd_per_mtok,
                cache_read_usd_per_mtok=model.cache_read_usd_per_mtok,
            )
            for model in catalog_models(services.prices)
        ],
    )
