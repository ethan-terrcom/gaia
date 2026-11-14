# Handoff: 07a — LLM clients & cost accounting

Filled from `HANDOFF_TEMPLATE.md` for the lead's sandboxed DeepSeek harness (2026-11-13): file tools
confined to the worktree, a sandboxed `run` tool (no network, no git writes, no installs). The lead
already added `anthropic>=2.8.0,<3` and `openai>=3.51.0,<4` with `uv add` (installed: anthropic 2.8.0,
openai 3.51.0), and does the commit and merge. The report goes through `finish`.

---

You are a junior engineer on **Gaia**, a local-first control plane for AI coding agents. Your task is
to implement exactly one work package, **07a — LLM clients & cost accounting**. The lead engineer
reviews your work, then commits and merges it. You never commit.

## 1. Read first, in this order
1. `docs/implementation/07-llm-providers.md`, everything **above** the "07b — LLM gateway" section. That is your contract.
2. `docs/implementation/00-README.md`: "Rules for every work package".
3. `docs/PROJECT_BRIEF.md` §5, `docs/ARCHITECTURE.md` §1, §5 (`llm_usage`), §6.
4. `docs/backend/README.md`, `docs/backend/database.md`, `docs/backend/config-and-secrets.md`, `docs/backend/python-concepts.md`.
5. Existing code you will import: `gaia/config/{settings,secrets}.py`, `gaia/db/{models,repo,engine}.py`,
   `gaia/domain/schemas.py` (`LlmUsageCreate`), `tests/conftest.py`, `tests/db_builders.py`.
6. The installed SDK sources: `backend/.venv/lib/python3.12/site-packages/anthropic/` and `.../openai/`.

Lead notes for this package:
- **Scope:** `gaia/llm/{base,anthropic_client,deepseek_client,fake,prices.toml}`, `gaia/llm/untrusted.py`
  (for `wrap_untrusted`), `gaia/services/costs.py`, tests, plus a `smoke-llm` Makefile target that runs a
  script you add at `backend/scripts/smoke_llm.py` (it uses the keychain store; `@pytest.mark.live`-style
  guard is not needed because it is never run by the test suite). You cannot run it: no network. The owner runs it.
- **Anthropic SDK facts (verified by the lead for anthropic 2.8.0, confirm in source):**
  - Model `claude-opus-5-5`: do not send `thinking` at all (or send `{"type": "adaptive"}`); never
    `budget_tokens` or `disabled`. Depth is `output_config={"effort": "high"}`. No `temperature`/`top_p`/`top_k`.
    No assistant prefill. `tool_choice` `any`/`tool` returns 400: use `auto`.
  - Refusal fallback: `client.beta.messages.create(..., betas=["server-side-fallback-2026-07-01"], fallbacks="default")`
    (check the beta resource's signature for the `fallbacks` parameter). Price by `message.model`, the model that served.
  - Streaming for large `max_tokens`: `async with client.messages.stream(...) as s: msg = await s.get_final_message()`
    (beta equivalent under `client.beta.messages.stream`).
  - Prompt caching: top-level `cache_control={"type": "ephemeral"}` on the request (confirm the parameter exists in 2.8.0's
    `messages.create` signature; if not, put `cache_control` on the last system text block).
  - Usage: `input_tokens`, `output_tokens`, `cache_read_input_tokens`, `cache_creation_input_tokens` (may be `None`).
  - Stop reasons to handle: `end_turn`, `tool_use`, `max_tokens`, `pause_turn`, `refusal` (`stop_details` may be set), `stop_sequence`.
  - Errors: `anthropic.RateLimitError`, `anthropic.APIStatusError`, `anthropic.APIConnectionError` (most specific first).
    The SDK retries twice already. Log `message._request_id`.
  - Opus 5.5 price per 1M tokens: input $4, output $20, cache read $0.20, cache write (5-min) $5.
- **DeepSeek facts (the lead observed them live today):** models `deepseek-v4-pro` and `deepseek-flash`; OpenAI-compatible
  `chat.completions.create` with `tools`. Responses may include `message.reasoning_content` (thinking); in a tool loop
  the lead's harness passes the assistant message back with `reasoning_content` and `tool_calls` intact. Usage includes
  `prompt_tokens`, `completion_tokens`, `prompt_cache_hit_tokens`, `prompt_cache_miss_tokens`. Read these fields defensively
  (`getattr`/`model_extra`), since they are not in the OpenAI types.
- **DeepSeek prices:** the lead could not verify current `deepseek-v4-pro`/`deepseek-flash` prices or peak/off-peak windows.
  Put clearly marked placeholder numbers in `prices.toml` with a `# VERIFY:` comment naming
  https://api-docs.deepseek.com/quick_start/pricing, and make the tests use their own price tables, never the shipped file.
- **`LLMStop`:** a typed exception for refusal / max_tokens (and an unexpected `pause_turn` you can't continue), carrying
  `stop_reason`, the refusal category if any, and the request id.
- **Recorder:** `UsageRecorder` protocol plus a DB implementation that writes `llm_usage` through `ScopedRepo` with a
  `Decimal` cost. `costs.py` holds pricing (`PriceTable.cost(usage, at=datetime)`), and `remaining_budget(run)`
  reading `run.budget_usd - run.cost_usd` is fine; budget *enforcement* is 07b/18a (leave `TODO(gaia-07b)`).
- **Keys:** constructors take a `SecretStore` (or a `SecretStr`), never read env vars. `require()` gives the fix message.
- **Tests:** no network. Fake the SDK clients (e.g. `httpx.MockTransport` passed via `http_client=`, or a stub object with
  the same method shape). 100% coverage of `gaia/` is the bar.
- Commands, from `backend/`: `uv run pytest -q`, `uv run mypy`, `uv run ruff check .`, `uv run ruff format .`, `uv run pytest --cov -q`.

## 2. What you may and must not touch
**You may create or edit only:** `backend/gaia/llm/**`, `backend/gaia/services/costs.py`, `backend/scripts/smoke_llm.py`,
`backend/tests/**`, the `smoke-llm` target in `Makefile` (add only that target and its `.PHONY` entry), and a
"Decisions and deviations (as built)" section you add to the end of the 07a part of `docs/implementation/07-llm-providers.md`
(above the 07b heading).

**You must not:** edit other docs, `.github/`, `.pre-commit-config.yaml`, `[tool.*]` sections of `backend/pyproject.toml`,
`uv.lock`, `gaia/security/**`, models or migrations; add dependencies; add suppressions without a reason comment; edit
existing tests to make them pass; build 07b (the gateway) or anything else out of scope.

## 3. Non-negotiable rules
- Secrets: none in code, tests or the report. Fake keys only, obviously fake (`sk-ant-test-...` will be redacted in logs, which is fine).
- No real network calls in tests. SQLAlchemy only; tenant tables via `ScopedRepo`.
- Library APIs: confirm against the installed source. List each in your report.

## 4. How to work
1. Plan first: files, public classes/functions with signatures, how each criterion is proven.
2. One task at a time, with tests.
3. Before finishing, all must pass: `uv run ruff check .`, `uv run ruff format --check .`, `uv run mypy`, `uv run pytest --cov -q` (100%).
4. Write the "Decisions and deviations (as built)" section.

## 5. When blocked
Stop and put the question under **Open questions** in your report, with what you have so far.

## 6. Report (the `finish` tool's `report` argument), in markdown
## Summary
## Plan vs. result
## Acceptance criteria   (each, with evidence)
## Deviations from the doc
## External APIs used   (library@version, API, source file checked)
## Security-relevant changes
## Tests added
## TODOs left for other packages
## Open questions
## Final check output   (last lines of ruff, mypy, pytest --cov)
