# 07 — LLM Providers & Cost Accounting

**Split (2026-11-12 review):** **07a** is everything below except the last section, and depends on
03 because the recorder writes `llm_usage` rows. **07b** (LLM gateway) is the last section
(approved as D2 on 2026-11-13).

## Goal
Thin, typed clients for Claude (lead) and DeepSeek (junior). Every call records its usage
and cost, and a fake provider makes the system testable without spending money.

## Out of scope
Agent loops and tools (09/10), and prompts (11).

## Files
- `gaia/llm/base.py`: `LLMUsage`, `PriceTable`, and the `UsageRecorder` protocol
- `gaia/llm/anthropic_client.py`
- `gaia/llm/deepseek_client.py`
- `gaia/llm/fake.py`
- `gaia/services/costs.py`

## Tasks
1. Add dependencies: `anthropic` (2.x at the time of writing; pin the major version), `openai` (used only
   for DeepSeek's OpenAI-compatible endpoint).
2. **Anthropic client.** Read the official Python SDK docs first; do not write calls from memory.
   - Use `anthropic.AsyncAnthropic(api_key=secrets.get(ANTHROPIC_API_KEY))`.
   - Model: `settings.lead_model` (default `claude-opus-5-5`).
   - On this model, thinking is always on, so **omit** `thinking` or send `{"type": "adaptive"}`.
     Never send `budget_tokens` or `{"type":"disabled"}`: both return a 400. Control depth with
     `output_config={"effort": "high"}` (the default on this model is `medium`, so set it explicitly).
   - Forced `tool_choice` (`any` or `tool`) returns a 400 on this model. Use `auto`, add `strict: true`
     to tool definitions, or use structured outputs (`client.messages.parse(..., output_format=PydanticModel)`).
   - Stream any request with a large `max_tokens` (`client.messages.stream(...)` + `get_final_message()`).
   - Turn on prompt caching for the stable system prompt (top-level `cache_control={"type":"ephemeral"}`).
   - Always check `stop_reason` before reading content, and handle `refusal`, `max_tokens` and `pause_turn`.
   - Refusals: Opus 5.5's classifiers can refuse security-heavy work, and Gaia's own tickets are
     security code. Enable server-side `fallbacks="default"` (beta
     `server-side-fallback-2026-07-01`) on planning and review calls, so a refusal retries on
     another model instead of failing the run. Price the usage by the model that actually served
     (`message.model`).
   - Error handling: catch the SDK's typed errors from most to least specific (`RateLimitError`,
     `APIStatusError`, `APIConnectionError`). The SDK already retries twice; don't wrap another retry loop around it.
   - Log `message._request_id` with every call.
3. **DeepSeek client.** `openai.AsyncOpenAI(api_key=..., base_url="https://api.deepseek.com")`. Use
   `chat.completions.create` with tools in OpenAI function-calling format. As of 2026-11 the models are
   `deepseek-v4-pro` and `deepseek-flash` (1M context, thinking on by default, tool calls supported).
   Check the current docs for how thinking-mode output must be passed back in multi-turn tool loops,
   and for the usage fields that report cache hits and misses. `base_url` comes from
   `settings.junior_base_url`.
4. Usage → cost: a `PriceTable` loaded from `backend/gaia/llm/prices.toml`, so prices can be edited
   without code changes. Per model, per 1M tokens: input, output, cache read and **cache write**
   (Anthropic bills `cache_creation_input_tokens` separately), with optional **time-of-day tiers**:
   DeepSeek charges peak and off-peak rates, with off-peak at half price, and separate cache-hit and
   cache-miss input rates. Record an `llm_usage` row on every call, including `price_tier`. Anthropic usage fields: `input_tokens`, `output_tokens`, `cache_read_input_tokens`,
   `cache_creation_input_tokens`.
5. `FakeLLM`: scripted responses for tests. It returns canned text or tool calls in sequence.
6. `make smoke-llm`: one tiny real call to each provider, printing tokens and cost. It is never run in CI.

## Security
- API keys are passed into the client constructor and never into env vars or logs.
- Wrap prompt content from repos and tickets in `<untrusted_content>` tags. Provide a helper here, `wrap_untrusted(text, source)`.

## Tests
- Cost is computed correctly for a sample usage object, including cache writes, and a DeepSeek call
  is priced differently at peak and off-peak times.
- The recorder writes `llm_usage` rows.
- A refusal or `max_tokens` stop surfaces as a typed `LLMStop` exception rather than empty text.

## Acceptance criteria
- [ ] `make smoke-llm` succeeds against both providers using keys from the keychain.

## Decisions and deviations (as built)
- **`PriceTable` lives in `gaia/services/costs.py`, not `gaia/llm/base.py`.** The doc's file list put
  it in `base.py`, but `base.py` must stay importable without pulling in SQLAlchemy models; putting the
  pricing class next to `load_price_table` and `DbUsageRecorder` avoids a circular import. `base.py`
  keeps `LLMUsage` and the `UsageRecorder` protocol.
- **`LLMUsage.input_tokens` is "tokens billed at the base input rate", not "total prompt tokens".**
  For Anthropic that is the SDK's `input_tokens` (cache reads/writes are separate fields). For DeepSeek
  it is `prompt_cache_miss_tokens` (or `prompt_tokens - prompt_cache_hit_tokens` when miss is absent),
  and `cache_read_tokens` is `prompt_cache_hit_tokens`. This lets one price formula work for both
  providers without adding columns to `llm_usage`.
- **DeepSeek prices and off-peak window are placeholders.** Every DeepSeek line in
  `gaia/llm/prices.toml` carries a `# VERIFY:` comment pointing at
  https://api-docs.deepseek.com/quick_start/pricing. The shipped table sets
  `off_peak_start_utc == off_peak_end_utc`, which `PriceTable` treats as "off-peak disabled" so Gaia
  never applies an unverified discount. Tests build their own `PriceTable` and never read the shipped
  file.
- **Anthropic request shape is intentionally narrow.** The client omits `thinking` (Opus 5.5 refuses
  `enabled`/`disabled` variants), sets `output_config={"effort": "high"}`, sends `tool_choice` as
  `{"type": "auto"}`, adds `strict: true` to tool definitions, and uses the top-level
  `cache_control={"type": "ephemeral"}` parameter that exists in anthropic 2.8.0.
- **Large requests stream at > 8192 `max_tokens`.** The doc says "large" without a number; 8192 is the
  client's explicit `STREAM_MAX_TOKENS_THRESHOLD` and is unit-tested on both sides of the boundary.
- **`make smoke-llm` is not part of the test suite.** It runs
  `backend/scripts/smoke_llm.py`, which reads the real keychain and makes two tiny live calls. CI and
  `pytest` never execute it.

- **Lead review fixes (2026-11-13, `docs/reviews/2026-11-13-review-07a.md`).** `tool_choice` is sent only
  with tools (the API rejects it otherwise). An unpriced serving model, as after a refusal fallback, is
  priced at the requested model's rates with `price_tier="priced-as-requested"`
  (`PriceTable.quote_or_price_as`), and clients refuse an unpriced model at construction
  (`PriceTable.require`). `LLMResponse.reasoning_content` carries DeepSeek's thinking output for tool
  loops. The smoke script allows 2048 output tokens.

---

## 07b — LLM gateway (D2)

### Goal
No agent process ever holds a provider key, and budgets are enforced at the one place every
model call passes through. Anthropic's
[secure deployment guide](https://code.claude.com/docs/en/agent-sdk/secure-deployment) recommends
this "proxy pattern" for agents.

### Files
- `gaia/llm/gateway.py`: the FastAPI router
- `gaia/llm/run_tokens.py`: minting, hashing and revoking per-run tokens

### Tasks
1. Routes `/llm/anthropic/{path}` and `/llm/deepseek/{path}` forward an **allowlist** of provider
   endpoints. Start with Anthropic `POST /v1/messages` and `/v1/messages/count_tokens`, and DeepSeek
   `POST /chat/completions`; ADR-001 adds whatever each chosen harness calls. Stream responses
   through unchanged, SSE included.
2. Auth is a **per-run token**, accepted as `x-api-key` (Anthropic-style clients) or `Authorization: Bearer`
   (OpenAI-style). It is minted when a runtime task starts, stored hashed, and revoked when the task
   ends. The gateway does not accept the admin `GAIA_API_TOKEN`, and `/api/v1` does not accept run tokens.
3. Strip the client's credential headers and inject the real key from the `SecretStore`.
4. Metering: read usage from the final response, or from the stream's usage event, and record
   `llm_usage` with `run_id`, `run_step_id` and `agent_id`.
5. Budget: call `costs.ensure_budget(run)` before forwarding. When the budget is exceeded, return a
   **non-retryable** error (HTTP 403, in the provider's own error shape) so harnesses stop instead of
   retrying, and publish a budget event.
6. Never log request or response bodies; log metadata only.

### Tests
- Responses and errors never contain the real key.
- Token scope: the admin token is rejected here, a run token is rejected on `/api/v1`, and a revoked
  token is rejected.
- Streaming passthrough is byte-identical, and usage is recorded for both streaming and non-streaming calls.
- An over-budget request gets the non-retryable error, and no upstream call is made.

### Acceptance criteria
- [ ] The native DeepSeek client and, in the spike, the Claude Agent SDK run through the gateway with
      no provider key in their environment.
