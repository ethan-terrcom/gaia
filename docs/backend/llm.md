# LLM clients & cost accounting

> Packages 07a, 19a · Source of truth: [`docs/implementation/07-llm-providers.md`](../implementation/07-llm-providers.md),
> [`19-agent-modes.md`](../implementation/19-agent-modes.md)
> · Concepts used: [async and await](python-concepts.md#async-and-await), [Protocols](python-concepts.md#protocols),
> [dataclasses](python-concepts.md#dataclasses-and-frozen-objects), [Pydantic `SecretStr`](python-concepts.md#pydantic-models-and-settings)

## What it's for

Gaia talks to four model providers: Anthropic (Claude), DeepSeek, OpenAI and xAI (Grok). This layer
wraps each provider's SDK in a thin client that speaks Gaia's own small types, prices every call,
and records it in `llm_usage`. A `FakeLLM` lets every other package test against scripted replies
without spending money.

Two adapters cover the four providers: `AnthropicClient` for Claude, and `OpenAICompatClient` for
the three that speak OpenAI's chat-completions format. Which model plays which role is the
owner's choice (doc 19); any priced model can be picked.

## Files

| File | Contents |
|---|---|
| `gaia/llm/base.py` | `LLMMessage`, `LLMTool`, `ToolCall`, `LLMResponse`, `LLMUsage`, `LLMStop`, the `UsageRecorder` protocol |
| `gaia/llm/anthropic_client.py` | `AnthropicClient` (lead model, `claude-opus-5-5`) |
| `gaia/llm/openai_compat_client.py` | `OpenAICompatClient(provider=…)` for DeepSeek, OpenAI and xAI, and its `DeepSeekClient` subclass. The only module that imports `openai` |
| `gaia/llm/providers.py` | `build_structured_llm` (lead calls) and `build_chat_llm` (native tool loop): provider name → adapter |
| `gaia/llm/catalog.py` | `catalog_models(prices)` and `provider_configured(store, provider)`: what `GET /models` shows |
| `gaia/llm/fake.py` | `FakeLLM`, scripted responses for tests |
| `gaia/llm/untrusted.py` | `wrap_untrusted(text, source)`, which marks repo or ticket text as data |
| `gaia/llm/prices.toml` | The model catalog: per model, its `provider`, `label`, `tier` and USD per 1M tokens, with DeepSeek's off-peak and the long-context tiers |
| `gaia/services/costs.py` | `PriceTable`, `load_price_table()`, `DbUsageRecorder`, `remaining_budget()` |
| `backend/scripts/smoke_llm.py` | `make smoke-llm`: one real call per provider, run by hand |

## How it works

```python
recorder = DbUsageRecorder(session, workspace_id, run_id, agent_id=lead.id)
client = AnthropicClient(api_key=store.require(SecretName.ANTHROPIC_API_KEY),
                         model=settings.lead_model, recorder=recorder,
                         price_table=load_price_table())
reply = await client.complete(system=PLAN_PROMPT, messages=[LLMMessage("user", ticket_text)],
                              enable_fallback=True)
```

1. The client converts Gaia messages and tools to the provider's wire format. Anthropic requests always
   set `output_config={"effort": "high"}`, never send `thinking` (Opus 5.5 always thinks), add
   `tool_choice: auto` only when there are tools, and mark the system prompt for caching.
   `enable_fallback=True` uses the beta server-side refusal fallback. Above 8192 `max_tokens` the
   request streams.
2. The response's token counts become an `LLMUsage` in four buckets: base input, output, cache read and
   cache write. For DeepSeek, "base input" is the cache-*miss* count; for OpenAI and xAI it is the
   prompt minus `prompt_tokens_details.cached_tokens`. Output is the larger of `completion_tokens`
   and `total_tokens − prompt_tokens`, because xAI reports reasoning tokens outside
   `completion_tokens` (`metering.openai_compat_buckets`).
3. `PriceTable` turns that into a `Decimal` cost. It picks the long-context tier when the prompt
   reaches a model's `from_prompt_tokens` (OpenAI over 272K, xAI 200K and up, Haiku 5.5 over 100K),
   otherwise DeepSeek's off-peak tier outside its weekday peak windows. The recorder writes the
   `llm_usage` row, scoped to the workspace.
4. **Only then** does the client check the stop reason. Refusal, `max_tokens`/`length`, `pause_turn` or a
   content filter raises `LLMStop`, never an empty reply. The call is billed either way, so it is always
   recorded first.

## Why it's built this way

- **Neutral types at the boundary.** Docs 10–12 depend on `LLMResponse`, not on either SDK, so swapping a
  provider or adding the gateway (07b) doesn't ripple outwards.
- **The recorder is bound at construction** (run, step, agent). A client never knows which run it
  serves, which is what lets 07b's gateway reuse it.
- **An unpriced serving model is estimated, not dropped.** After a refusal fallback, `message.model` may be
  a model `prices.toml` doesn't list. The row keeps the real model, and `price_tier="priced-as-requested"`
  marks the cost as an estimate. A client built for an unpriced model fails at construction, before any
  spend.
- **`reasoning_content` round-trips.** DeepSeek's thinking output comes back on `LLMResponse`, and must
  go back on the assistant `LLMMessage` in a tool loop. Only the DeepSeek dialect sends it back.
- **One OpenAI-compatible adapter, three dialects.** DeepSeek takes `max_tokens`; OpenAI's reasoning
  models reject it and take `max_completion_tokens`, which xAI accepts too. Structured output is JSON
  mode plus the schema in the system prompt for all three: the one form every one of them accepts.

## Changing it safely

- **A new model:** add a `[models.<id>]` section to `prices.toml` with `provider`, `label`, `tier`
  and its prices (and a `long_context` table if the provider has one). Ids with a dot are quoted
  TOML keys (`[models."gpt-6.2-sol"]`). Nothing else changes: it appears in `GET /models` and can be
  picked for a role.
- **A new OpenAI-compatible provider:** add it to `LLMProvider`, `PROVIDER_SECRETS`,
  `Settings.provider_base_url` and the gateway's `_ALLOWLISTS`, plus a route; a dialect switch in
  `OpenAICompatClient` only if it differs.
- **Tests** stub the SDK client through the `client=` constructor argument (see
  `tests/test_anthropic_client.py`). They never use the network and never read the shipped price file.
- **Real calls:** `make smoke-llm` needs `ANTHROPIC_API_KEY` and `DEEPSEEK_API_KEY` in the keychain
  (`gaia secrets set …`). `OPENAI_API_KEY` and `XAI_API_KEY` are set the same way.

Agents never call providers directly: they go through the [LLM gateway](gateway.md) (07b), which
reuses this module's metering and pricing.
