# 07 — LLM Providers & Cost Accounting

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
1. Add dependencies: `anthropic`, `openai` (used only for DeepSeek's OpenAI-compatible endpoint).
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
   - Error handling: catch the SDK's typed errors from most to least specific (`RateLimitError`,
     `APIStatusError`, `APIConnectionError`). The SDK already retries twice; don't wrap another retry loop around it.
   - Log `message._request_id` with every call.
3. **DeepSeek client.** `openai.AsyncOpenAI(api_key=..., base_url="https://api.deepseek.com")`. Use
   `chat.completions.create` with tools in OpenAI function-calling format. Check DeepSeek's current docs
   for model names, context limits and tool-calling support.
4. Usage → cost: a `PriceTable` loaded from `backend/gaia/llm/prices.toml` (per-model input, output and
   cache-read prices per 1M tokens), so prices can be edited without code changes. Record an `llm_usage`
   row on every call. Anthropic usage fields: `input_tokens`, `output_tokens`, `cache_read_input_tokens`,
   `cache_creation_input_tokens`.
5. `FakeLLM`: scripted responses for tests. It returns canned text or tool calls in sequence.
6. `make smoke-llm`: one tiny real call to each provider, printing tokens and cost. It is never run in CI.

## Security
- API keys are passed into the client constructor and never into env vars or logs.
- Wrap prompt content from repos and tickets in `<untrusted_content>` tags. Provide a helper here, `wrap_untrusted(text, source)`.

## Tests
- Cost is computed correctly for a sample usage object.
- The recorder writes `llm_usage` rows.
- A refusal or `max_tokens` stop surfaces as a typed `LLMStop` exception rather than empty text.

## Acceptance criteria
- [ ] `make smoke-llm` succeeds against both providers using keys from the keychain.
