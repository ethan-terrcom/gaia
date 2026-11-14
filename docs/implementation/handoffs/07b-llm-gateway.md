# Handoff: 07b — LLM gateway (D2)

Filled from `HANDOFF_TEMPLATE.md` for the lead's sandboxed DeepSeek harness (2026-11-14). File tools
are confined to the worktree. The `run` tool is sandboxed: no network, no git writes, no installs. The
lead does the commit and merge, and the report goes through `finish`.

---

You are a junior engineer on **Gaia**, a local-first control plane for AI coding agents. Your task is
to implement exactly one work package, **07b — LLM gateway**. The lead engineer reviews your work,
then commits and merges it. You never commit.

## 1. Read first, in this order
1. `docs/implementation/07-llm-providers.md`, the **07b section** at the end. That section is your
   contract; the rest of the doc is background (07a is merged).
2. `docs/implementation/00-README.md`, "Rules for every work package".
3. `docs/ARCHITECTURE.md` §3 and §7, and `docs/reviews/2026-11-12-plan-review.md`, the D2 section.
4. `docs/backend/README.md`, then `docs/backend/llm.md`, `api.md`, `config-and-secrets.md`,
   `database.md` and `python-concepts.md`.
5. `docs/reviews/2026-11-13-review-07a.md` and `docs/reviews/2026-11-13-review-06.md`, which cover what
   just changed.
6. The code you build on:
   - `gaia/llm/{base,anthropic_client,deepseek_client}.py`
   - `gaia/services/costs.py`
   - `gaia/api/{app,deps,errors,auth}.py`
   - `gaia/config/{secrets,settings}.py`
   - `gaia/security/redaction.py`
   - `gaia/db/{models,repo}.py`
   - `alembic/versions/`
   - `tests/{conftest,db_builders}.py`, `tests/test_api_*.py`, `tests/test_costs.py`

## 2. Lead notes for this package
These are design decisions already made. Follow them, and record any deviation.

**Run tokens (`gaia/llm/run_tokens.py`, new table `run_token`, migration `0004`).**
- Add the model `RunToken(UUIDPrimaryKey, WorkspaceScoped, Timestamps, Base)` with these columns:
  - `run_id` (FK `run.id`, `ondelete=CASCADE`, indexed)
  - `run_step_id` (nullable FK `run_step.id`, `SET NULL`)
  - `agent_id` (nullable FK `agent.id`, `SET NULL`)
  - `provider` (`String(16)`: `"anthropic"` or `"deepseek"`)
  - `token_hash` (`String(64)`, **unique**)
  - `expires_at` (`UtcDateTime`)
  - `revoked_at` (`UtcDateTime`, nullable)
- The migration is `0004`, revises `0003`, and uses batch mode. Generate it with
  `uv run alembic revision --autogenerate -m "run tokens" --rev-id 0004`, then read and tidy it.
- `mint(session, *, workspace_id, run_id, provider, ttl_s, run_step_id=None, agent_id=None) -> MintedToken`.
  - The plaintext is `"gaia_rt_" + secrets.token_urlsafe(32)`.
  - Store `sha256(plaintext).hexdigest()`. A fast hash is right here: the token has 256 bits of
    entropy, so brute force is not the threat.
  - Call `register_secret(plaintext)` so it is redacted from logs.
  - `MintedToken` is a frozen dataclass holding `token: SecretStr`, `token_id` and `expires_at`.
  - The plaintext is returned once and never stored.
- `resolve(session, presented) -> RunTokenContext | None` returns `None` when the token is unknown,
  revoked or expired, or when its run is in a terminal status (`completed`, `failed`, `cancelled`).
  - Look the token up by hash.
  - Look up the run **unscoped by workspace** through a small `RunTokenRepo` in `gaia/db/repo.py`.
    The token itself is what proves the workspace, so read `workspace_id` from the token row and then
    load the run with `ScopedRepo(..., workspace_id)`.
  - `RunTokenContext` is a frozen dataclass of `token_id`, `workspace_id`, `run_id`, `run_step_id`,
    `agent_id` and `provider`.
- `revoke(session, token_id)` and `revoke_for_run(session, workspace_id, run_id)` set `revoked_at`.
  Both are idempotent.

**Gateway router (`gaia/llm/gateway.py`).**
- Mount the router at `/llm` in `create_app`, **outside** `api_v1_router()`, because it has its own
  auth.
- Routes are `POST /llm/anthropic/{path:path}` and `POST /llm/deepseek/{path:path}`.
- Allowlist, matched exactly on the normalised path with no leading slash:
  - anthropic: `v1/messages` and `v1/messages/count_tokens`;
  - deepseek: `chat/completions`, `v1/chat/completions` and `anthropic/v1/messages`. The last is
    DeepSeek's Anthropic-compatible endpoint; the doc 08 spike needs it.
- Reject anything else with 404 in the provider's error shape. Any method other than POST gets 405.
- **Auth** takes the run token from `x-api-key` or `Authorization: Bearer` (reuse `bearer_token()`
  from `gaia/api/auth.py`).
  - A missing or unknown token gets 401.
  - So does a token whose `provider` does not match the route.
  - The admin `GAIA_API_TOKEN` is just an unknown token here. Test that it gets 401.
  - Also prove that `/api/v1/session` rejects a run token with 401. `LocalTokenAuth` already only
    accepts the admin token, so this needs only a test.
- **Upstream base URLs:** anthropic `https://api.anthropic.com`; deepseek `settings.junior_base_url`.
  Add `Settings.anthropic_base_url: str = "https://api.anthropic.com"` too, so tests and the spike can
  point elsewhere.
- **Request headers:** forward only an allowlist: `content-type`, `accept`, `anthropic-version`,
  `anthropic-beta` and `user-agent`.
  - Drop everything else, including `authorization`, `x-api-key`, `cookie`, `host`, `x-forwarded-*`
    and `accept-encoding`.
  - Send `accept-encoding: identity`, so the bytes you relay are the bytes you parse.
  - Inject the real key: `x-api-key` for anthropic, `Authorization: Bearer <key>` for deepseek.
  - Read the key from `SecretStore` with `run_in_threadpool`, because keyring is a blocking OS call.
  - A missing key gets 503, "provider not configured", in the provider's shape.
- **Request body.**
  - Cap it at 20 MB; anything larger gets 413.
  - Parse it as JSON. Invalid JSON gets 400.
  - `model` must be a string the `PriceTable` knows (`require`). Otherwise return 400: the gateway
    must be able to price every call it forwards.
  - For DeepSeek streaming (`"stream": true`), set `stream_options.include_usage = true` if it is
    absent, so the final chunk carries usage. That is the only body change; note it in the doc's
    deviations.
- **Budget**, checked before forwarding:
  - Add `async def ensure_budget(session, run, settings, *, now) -> None` to `gaia/services/costs.py`.
  - It raises `BudgetExceeded(kind, limit_usd, spent_usd)` when the run budget is used up
    (`remaining_budget(run) <= 0`), or when today's (UTC) total `llm_usage.cost_usd` for the
    workspace is ≥ `settings.daily_budget_usd`.
  - Put the daily sum query in `gaia/db/repo.py`.
  - Resolve the existing `TODO(gaia-07b)`.
  - The gateway maps `BudgetExceeded` to **HTTP 403** in the provider's error shape, with no upstream
    call.
    - Anthropic: `{"type":"error","error":{"type":"permission_error","message":"..."}}`.
    - OpenAI/DeepSeek: `{"error":{"message":"...","type":"budget_exceeded","code":"budget_exceeded"}}`.
  - It also appends an `event` row of type `run.budget_exceeded` through `EventRepo`, with a small
    payload (`kind`, `limit_usd`, `spent_usd`). Mark it `# TODO(gaia-13): publish through EventBus`,
    because doc 13 is being built in parallel.
- **Forwarding** goes through one process-wide `httpx.AsyncClient`.
  - Build it in the lifespan with `timeout=httpx.Timeout(connect=10, read=600, write=60, pool=10)`
    and close it on shutdown.
  - Store it on `AppServices` as `llm_http`, and the loaded `PriceTable` as `prices`.
  - Let `create_app(settings, *, llm_transport: httpx.AsyncBaseTransport | None = None)` accept a
    transport, so tests can pass `httpx.MockTransport`. Production passes none.
  - Use `client.build_request(...)` plus `client.send(request, stream=True)`.
- **Responses** keep the upstream status code.
  - Copy only an allowlist of headers: `content-type`, `request-id`, `x-request-id`, `retry-after`,
    `anthropic-ratelimit-*` and `x-ratelimit-*`.
  - Relay the body with `StreamingResponse` over `response.aiter_raw()`, **unchanged**. That covers
    both SSE and non-streaming JSON.
  - Close the upstream response in `finally`, including on client disconnect.
  - An upstream connection error or timeout gets 502 in the provider's shape. The message names the
    exception class only, never `str(exc)`.
- **Metering** (new `gaia/llm/metering.py`, no I/O). Build a `UsageAccumulator` per request that is
  fed every relayed chunk and yields `LLMUsage | None` at the end:
  - Anthropic JSON body: `usage` and `model`.
  - Anthropic SSE:
    - `message_start` → `message.usage` and `message.model`;
    - `message_delta` → `usage.output_tokens`. This value is cumulative: take the last one.
  - OpenAI/DeepSeek JSON body, or the SSE chunk that has `usage`: take `usage` and `model`.
    `input_tokens` is the cache-miss count, as in 07a's `deepseek_client`; reuse or extract its
    helpers rather than duplicating the rules.
  - `count_tokens` is never metered.
  - DeepSeek's `anthropic/v1/messages` uses the Anthropic parser, and the row's provider is `deepseek`.
- **Recording.** After the body finishes (the generator's `finally`), open a **new** session from
  `services.sessions` and record through `DbUsageRecorder`, with the token's run, step and agent.
  - Price with `PriceTable.quote_or_price_as(usage, at=now, fallback_model=<requested model>)`.
  - Change `DbUsageRecorder.record` so it also adds the cost to `run.cost_usd` **atomically** in the
    same transaction: an `UPDATE run SET cost_usd = cost_usd + :c`, not read-modify-write. Put it in
    `db/repo.py`.
  - If no usage could be parsed from a 2xx response, record a zero-token row with
    `price_tier="unmetered"` and log a warning. Do not crash the stream.
- **Logging:** one `llm.gateway` line per call, with `run_id`, `provider`, `path`, `model`, `status`,
  `duration_ms`, the upstream request id and `streamed`. Never log headers or bodies.
- **Dev helper:** `backend/scripts/dev_run_token.py`, so the lead can run the acceptance criterion
  live.
  - Given `GAIA_DATA_DIR`, it seeds a scratch repository, ticket and run (budget from settings).
  - It mints a token for `--provider` with `--ttl` (default 3600 s), then prints only the token and
    the run id.
  - It never prints a provider key. The repository row may point at any directory, for example
    `--repo-path`, defaulting to the current directory; validate it through `GitService`.

## 3. What you may and must not touch
**You may create or edit only:**
- `backend/gaia/llm/{gateway,run_tokens,metering}.py`, and `backend/gaia/llm/deepseek_client.py` and
  `anthropic_client.py` only to extract shared usage helpers into `metering.py`;
- `backend/gaia/services/costs.py`;
- `backend/gaia/api/{app,deps,errors}.py`;
- `backend/gaia/config/settings.py` (the one new field);
- `backend/gaia/db/{models,repo}.py` and `backend/alembic/versions/0004_*.py`;
- `backend/scripts/dev_run_token.py`;
- `backend/tests/**`;
- a "Decisions and deviations (as built)" subsection at the end of the 07b section of
  `docs/implementation/07-llm-providers.md` (tick its acceptance criterion only if you can prove it;
  you cannot run a live server, so say so).

**You must not:**
- edit other docs, `.github/`, `.pre-commit-config.yaml`, the `[tool.*]` sections of `pyproject.toml`,
  `uv.lock`, `gaia/security/**`, `gaia/services/{git,proc,worktrees}.py` or existing migrations;
- add dependencies (httpx is already one);
- make real network calls in tests;
- add suppressions without a reason comment;
- build doc 13's event bus or doc 10's runtimes.

## 4. Non-negotiable rules
- **Secrets:** none in code, tests or the report, and never print environment variables. Tests use
  fake keys such as `"sk-ant-test-" + "x" * 40` in an `InMemorySecretStore`. Assert that the real key
  never appears in a response body, a response header or a captured log line, for success, upstream
  error, connection error and budget refusal.
- **SQL:** SQLAlchemy expressions only, inside `gaia/db/`.
- **Library APIs:** confirm against the installed source under
  `backend/.venv/lib/python3.12/site-packages/` (httpx 0.28 `AsyncClient.send(stream=True)`,
  `Response.aiter_raw`, `MockTransport`; Starlette `StreamingResponse`; FastAPI routing).

## 5. How to work
1. Plan first: the files, the public functions with signatures, and how each Test in the doc is
   proven.
2. Build it in order: run tokens and migration, then metering (pure, table-driven tests), then the
   budget, then the router.
3. **Streaming tests.** `httpx.MockTransport` can return a streaming body: build an
   `httpx.Response(200, headers=..., stream=<an AsyncByteStream yielding chunks>)`.
   - Prove byte-identical passthrough by comparing the client-side bytes with the concatenated
     upstream chunks.
   - Split an SSE event across two chunks, to prove the parser buffers lines.
4. **Coverage.** Code that runs inside a `StreamingResponse` generator is covered normally, but
   coverage does not trace greenlets. If a line in a session-using path shows as missed, add a direct
   test of that function.
5. Before you finish, all of these must pass, run from `backend/`:
   `uv run ruff check .`, `uv run ruff format --check .`, `uv run mypy` and `uv run pytest --cov -q`
   (100% coverage of `gaia/`). Also run `uv run alembic upgrade head` against a temporary
   `GAIA_DATA_DIR`.
6. Write the "Decisions and deviations (as built)" subsection.

## 6. When blocked
Stop and put the question under **Open questions** in your report, with what you have so far.

## 7. Report (the `finish` tool's `report` argument), in markdown
```
## Summary
## Plan vs. result
## Tests in the doc   (each, with the test name that proves it)
## Endpoints and error shapes
## Deviations from the doc
## External APIs used   (library@version, API, source file checked)
## Security-relevant changes
## Tests added
## TODOs left for other packages
## Open questions
## Final check output   (last lines of ruff, mypy, pytest --cov)
```
