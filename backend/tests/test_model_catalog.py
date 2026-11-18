"""The model catalog: prices.toml as the registry, provider keys, ``GET /models`` (19a)."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from gaia.config.secrets import InMemorySecretStore, SecretName, provider_secret
from gaia.config.settings import Settings
from gaia.domain.enums import LLMProvider, ModelTier
from gaia.llm.base import LLMUsage
from gaia.llm.catalog import catalog_models, provider_configured
from gaia.llm.metering import openai_compat_buckets, openai_usage_from_payload
from gaia.orchestrator.auto_mode import (
    AUTO_MODE_FILE,
    AutoModeState,
    AutoModeSwitch,
    ModelFamily,
    family_for_provider,
)
from gaia.services.costs import (
    LongContextRates,
    ModelPrice,
    PriceRates,
    PriceTable,
    load_price_table,
)
from tests.conftest import Api

NOON = datetime(2026, 11, 17, 12, 0, tzinfo=UTC)


# --- prices.toml as the catalog --------------------------------------------


def test_every_shipped_model_names_its_provider_label_and_tier() -> None:
    table = load_price_table()
    providers = set()
    for model in table.model_names():
        price = table.get(model)
        assert price is not None
        assert price.provider in {provider.value for provider in LLMProvider}, model
        assert price.label, model
        assert price.tier in {tier.value for tier in ModelTier}, model
        providers.add(price.provider)

    assert providers == {provider.value for provider in LLMProvider}


@pytest.mark.parametrize(
    ("model", "threshold"),
    [("gpt-6.2-astra", 272_001), ("grok-4.7", 200_000), ("claude-haiku-5-5", 100_001)],
)
def test_shipped_long_context_tiers_bill_the_whole_request(model: str, threshold: int) -> None:
    table = load_price_table()
    short = LLMUsage(provider="x", model=model, input_tokens=threshold - 1)
    long = replace(short, input_tokens=threshold)

    short_quote = table.quote(short, at=NOON)
    long_quote = table.quote(long, at=NOON)
    per_token_short = short_quote.cost_usd / (threshold - 1)
    per_token_long = long_quote.cost_usd / threshold

    assert short_quote.price_tier is None
    assert long_quote.price_tier == "long-context"
    # Each shipped long-context input rate is at least double the base rate.
    assert per_token_long >= per_token_short * 2


def test_long_context_counts_cache_reads_and_writes_toward_the_threshold() -> None:
    table = PriceTable(
        {
            "m": ModelPrice(
                rates=PriceRates(input=Decimal("1"), output=Decimal("1")),
                long_context=LongContextRates(
                    from_prompt_tokens=10,
                    rates=PriceRates(input=Decimal("2"), output=Decimal("2")),
                ),
            )
        }
    )
    usage = LLMUsage(
        provider="openai", model="m", input_tokens=4, cache_read_tokens=3, cache_creation_tokens=3
    )

    assert table.quote(usage, at=NOON).price_tier == "long-context"


def _toml(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "prices.toml"
    path.write_text(body, encoding="utf-8")
    return path


def test_catalog_fields_are_parsed(tmp_path: Path) -> None:
    table = load_price_table(
        _toml(
            tmp_path,
            '[models.m]\nprovider = "xai"\nlabel = "M"\ntier = "fast"\ninput = 1\noutput = 2\n'
            "[models.m.long_context]\nfrom_prompt_tokens = 5\ninput = 3\noutput = 4\n",
        )
    )

    price = table.get("m")
    assert price is not None
    assert (price.provider, price.label, price.tier) == ("xai", "M", "fast")
    assert price.long_context == LongContextRates(
        from_prompt_tokens=5, rates=PriceRates(input=Decimal("3"), output=Decimal("4"))
    )
    assert table.provider_of("m") == "xai"
    assert table.provider_of("missing") is None
    assert table.get("missing") is None


@pytest.mark.parametrize(
    ("extra", "message"),
    [
        ('provider = "gemini"', "provider"),
        ("provider = 3", "provider"),
        ('tier = "huge"', "tier"),
        ('label = ""', "label"),
        ("label = 7", "label"),
    ],
)
def test_bad_catalog_fields_are_rejected(tmp_path: Path, extra: str, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        load_price_table(_toml(tmp_path, f"[models.m]\ninput = 1\noutput = 1\n{extra}\n"))


@pytest.mark.parametrize("threshold", ["0", "-5", "true", '"many"'])
def test_bad_long_context_thresholds_are_rejected(tmp_path: Path, threshold: str) -> None:
    body = (
        "[models.m]\ninput = 1\noutput = 1\n"
        f"[models.m.long_context]\nfrom_prompt_tokens = {threshold}\ninput = 1\noutput = 1\n"
    )
    with pytest.raises(ValueError, match="from_prompt_tokens"):
        load_price_table(_toml(tmp_path, body))


def test_catalog_models_skip_unassigned_models_and_sort_by_provider_then_tier() -> None:
    rates = PriceRates(input=Decimal("1"), output=Decimal("2"), cache_read=Decimal("0.5"))
    table = PriceTable(
        {
            "z-fast": ModelPrice(rates=rates, provider="xai", tier="fast"),
            "a-frontier": ModelPrice(rates=rates, provider="xai", tier="frontier", label="A"),
            "untiered": ModelPrice(rates=rates, provider="xai"),
            "claude": ModelPrice(rates=rates, provider="anthropic", tier="mid"),
            "legacy": ModelPrice(rates=rates),
        }
    )

    models = catalog_models(table)

    assert [model.id for model in models] == ["claude", "a-frontier", "z-fast", "untiered"]
    assert models[1].label == "A"
    assert models[2].label == "z-fast"
    assert models[0].cache_read_usd_per_mtok == Decimal("0.5")


def test_provider_configured_checks_presence_only() -> None:
    store = InMemorySecretStore()

    assert provider_configured(store, "openai") is False
    store.set(SecretName.OPENAI_API_KEY, "sk-openai-test-123")
    assert provider_configured(store, "openai") is True
    assert provider_configured(InMemorySecretStore(), "xai", fake_llm=True) is True


def test_every_provider_has_a_keychain_entry_and_a_base_url() -> None:
    settings = Settings()

    assert provider_secret("xai") is SecretName.XAI_API_KEY
    assert settings.provider_base_url("openai") == "https://api.openai.com/v1"
    assert settings.provider_base_url("xai") == "https://api.x.ai/v1"
    assert settings.provider_base_url("deepseek") == settings.junior_base_url
    assert settings.provider_base_url("anthropic") == settings.anthropic_base_url
    with pytest.raises(ValueError, match="gemini"):
        provider_secret("gemini")


# --- usage buckets ---------------------------------------------------------


def test_openai_cached_tokens_become_cache_reads() -> None:
    assert openai_compat_buckets(
        {
            "prompt_tokens": 100,
            "completion_tokens": 10,
            "total_tokens": 110,
            "prompt_tokens_details": {"cached_tokens": 30},
        }
    ) == (70, 30, 10)


def test_deepseek_fields_win_over_cached_details() -> None:
    assert openai_compat_buckets(
        {
            "prompt_tokens": 100,
            "completion_tokens": 10,
            "prompt_cache_hit_tokens": 80,
            "prompt_cache_miss_tokens": 20,
            "prompt_tokens_details": {"cached_tokens": 5},
        }
    ) == (20, 80, 10)


def test_reasoning_outside_completion_tokens_counts_as_output() -> None:
    usage = openai_usage_from_payload(
        {
            "model": "grok-4.7",
            "usage": {"prompt_tokens": 32, "completion_tokens": 9, "total_tokens": 135},
        },
        provider="xai",
    )

    assert usage is not None
    assert (usage.input_tokens, usage.output_tokens) == (32, 103)


def test_a_details_field_that_is_not_a_dict_is_ignored() -> None:
    assert openai_compat_buckets(
        {"prompt_tokens": 5, "completion_tokens": 1, "prompt_tokens_details": "n/a"}
    ) == (5, 0, 1)


# --- auto-mode families ----------------------------------------------------


def test_every_provider_maps_to_an_auto_mode_family() -> None:
    assert [family_for_provider(provider.value) for provider in LLMProvider] == [
        ModelFamily.CLAUDE,
        ModelFamily.DEEPSEEK,
        ModelFamily.OPENAI,
        ModelFamily.XAI,
    ]


def test_openai_and_xai_switches_round_trip(tmp_path: Path) -> None:
    switch = AutoModeSwitch(tmp_path / AUTO_MODE_FILE)

    switch.write(AutoModeState(openai=True))

    assert switch.read() == AutoModeState(openai=True)
    assert switch.enabled(ModelFamily.OPENAI) is True
    assert switch.enabled(ModelFamily.XAI) is False


# --- GET /models -----------------------------------------------------------


@pytest.mark.anyio
async def test_models_route_lists_providers_and_marks_availability(api: Api) -> None:
    api.store.set(SecretName.OPENAI_API_KEY, "sk-openai-test-123")

    response = await api.client.get("/api/v1/models", headers=api.auth)

    assert response.status_code == 200
    body = response.json()
    providers = {entry["name"]: entry for entry in body["providers"]}
    assert providers["openai"] == {
        "name": "openai",
        "configured": True,
        "secret_name": "OPENAI_API_KEY",
    }
    assert providers["xai"]["configured"] is False
    models = {entry["id"]: entry for entry in body["models"]}
    assert models["gpt-6.2-sol"]["available"] is True
    assert models["gpt-6.2-sol"]["tier"] == "mid"
    assert models["grok-4.7"]["available"] is False
    assert models["claude-opus-5-5"]["label"] == "Claude Opus 5.5"
    assert "sk-openai-test-123" not in response.text


@pytest.mark.anyio
async def test_models_route_shows_untiered_models(api: Api) -> None:
    services = api.app.state.services
    rates = PriceRates(input=Decimal("1"), output=Decimal("1"))
    api.app.state.services = replace(
        services, prices=PriceTable({"m": ModelPrice(rates=rates, provider="xai")})
    )

    response = await api.client.get("/api/v1/models", headers=api.auth)

    assert response.json()["models"] == [
        {
            "id": "m",
            "provider": "xai",
            "label": "m",
            "tier": None,
            "available": False,
            "input_usd_per_mtok": "1",
            "output_usd_per_mtok": "1",
            "cache_read_usd_per_mtok": "0",
        }
    ]


@pytest.mark.anyio
async def test_models_route_requires_the_token(api: Api) -> None:
    response = await api.client.get("/api/v1/models")

    assert response.status_code == 401
