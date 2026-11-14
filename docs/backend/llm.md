# LLM clients & cost accounting

> Package 07a · Source of truth: [`docs/implementation/07-llm-providers.md`](../implementation/07-llm-providers.md)
> · Concepts used: [async and await](python-concepts.md#async-and-await), [Protocols](python-concepts.md#protocols),
> [dataclasses](python-concepts.md#dataclasses-and-frozen-objects), [Pydantic `SecretStr`](python-concepts.md#pydantic-models-and-settings)

## What it's for

Gaia talks to two model providers: Claude (Anthropic) for the lead engineer and DeepSeek for the
juniors. This layer wraps each provider's SDK in a thin client that speaks Gaia's own small types,
prices every call, and records it in `llm_usage`. A `FakeLLM` lets every other package test against
scripted replies without spending money.

## Files

| File | Contents |
|---|---|
| `gaia/llm/base.py` | `LLMMessage`, `LLMTool`, `ToolCall`, `LLMResponse`, `LLMUsage`, `LLMStop`, the `UsageRecorder` protocol |
| `gaia/llm/anthropic_client.py` | `AnthropicClient` (lead model, `claude-opus-5-5`) |
| `gaia/llm/deepseek_client.py` | `DeepSeekClient` (OpenAI-compatible endpoint) |
| `gaia/llm/fake.py` | `FakeLLM`, scripted responses for tests |
| `gaia/llm/untrusted.py` | `wrap_untrusted(text, source)`, which marks repo or ticket text as data |
| `gaia/llm/prices.toml` | USD per 1M tokens, per model (DeepSeek values still to be verified) |
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
   cache write. For DeepSeek, "base input" is the cache-*miss* count.
3. `PriceTable` turns that into a `Decimal` cost, picking DeepSeek's off-peak tier when enabled. The
   recorder writes the `llm_usage` row, scoped to the workspace.
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
  go back on the assistant `LLMMessage` in a tool loop.

## Changing it safely

- **A new model:** add its prices to `prices.toml` first, or the client will refuse to start.
- **Tests** stub the SDK client through the `client=` constructor argument (see
  `tests/test_anthropic_client.py`). They never use the network and never read the shipped price file.
- **Real calls:** `make smoke-llm` needs `ANTHROPIC_API_KEY` and `DEEPSEEK_API_KEY` in the keychain
  (`gaia secrets set …`).
