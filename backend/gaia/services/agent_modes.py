"""Checking agent-mode configs and routing their models to runtimes (doc 19b).

Three questions are answered here, and nowhere else:

- Is every model in a config one Gaia can use? (:func:`catalog_problems`)
- Is every provider it needs configured? (:func:`missing_provider_keys`)
- Which runtime runs an agent session on a given model? (:func:`runtime_for_model`)
"""

from __future__ import annotations

from typing import Any

from gaia.config.secrets import SecretName, SecretStore, provider_secret
from gaia.config.settings import Settings
from gaia.domain.agent_modes import AgentModeConfig, config_from_json, default_agent_config
from gaia.domain.enums import LLMProvider, RuntimeKind
from gaia.llm.catalog import provider_configured
from gaia.services.costs import PriceTable


class UnknownModel(ValueError):
    """A model is not in the catalog (``prices.toml`` with a ``provider``)."""

    def __init__(self, model: str) -> None:
        self.model = model
        super().__init__(f"model {model!r} is not in the model catalog (prices.toml)")


def provider_of_model(model: str, prices: PriceTable) -> str:
    """Return the provider that serves ``model``, or raise :class:`UnknownModel`."""
    provider = prices.provider_of(model)
    if provider is None:
        raise UnknownModel(model)
    return provider


def catalog_problems(config: AgentModeConfig, prices: PriceTable) -> list[str]:
    """One message per model in ``config`` that the catalog does not list."""
    return [
        f"model {model!r} is not in the model catalog"
        for model in config.models()
        if prices.provider_of(model) is None
    ]


def missing_provider_keys(
    config: AgentModeConfig,
    prices: PriceTable,
    store: SecretStore,
    *,
    fake_llm: bool = False,
) -> list[SecretName]:
    """The keychain entries ``config`` needs that are not set, in a stable order.

    Only presence is checked. Demo mode needs none: the gateway answers every
    provider with the local fake.
    """
    providers = {provider_of_model(model, prices) for model in config.models()}
    return [
        provider_secret(provider.value)
        for provider in LLMProvider
        if provider.value in providers
        and not provider_configured(store, provider.value, fake_llm=fake_llm)
    ]


def legacy_agent_config(settings: Settings, junior_runtime: str | None) -> AgentModeConfig:
    """The config a run without one gets: the behaviour before 19b.

    The lead is ``planner_model`` (which also took escalations when the lead
    runtime was native, as in the swap drill). A run started with the old
    ``junior_runtime=claude_agent_sdk`` override ran its steps on the Claude
    runtime, whose model was ``lead_model``.
    """
    worker = (
        settings.lead_model
        if junior_runtime == RuntimeKind.CLAUDE_AGENT_SDK.value
        else settings.junior_model
    )
    return default_agent_config(lead_model=settings.planner_model, junior_model=worker)


def run_agent_config(
    stored: Any, *, settings: Settings, junior_runtime: str | None
) -> AgentModeConfig:
    """The config a run executes with: the one it stored, else the legacy one."""
    config = config_from_json(stored)
    if config is not None:
        return config
    return legacy_agent_config(settings, junior_runtime)


def runtime_for_model(
    model: str, prices: PriceTable, settings: Settings
) -> tuple[RuntimeKind, str]:
    """Return the runtime that runs an agent session on ``model``, and the model it uses.

    Anthropic models run on the Claude Agent SDK runtime; every other provider
    runs on Gaia's native loop. Demo mode never starts the ``claude`` CLI, so an
    Anthropic session there runs on the native runtime with the demo junior
    model instead (the same override ``Settings`` applies to the old settings).
    """
    if provider_of_model(model, prices) != LLMProvider.ANTHROPIC.value:
        return RuntimeKind.NATIVE_DEEPSEEK, model
    if settings.fake_llm:
        return RuntimeKind.NATIVE_DEEPSEEK, settings.junior_model
    return RuntimeKind.CLAUDE_AGENT_SDK, model
