# The LLM gateway

> Package **07b** (owner decision D2) · `gaia/llm/{gateway,run_tokens,metering}.py`,
> `gaia/services/costs.py` (`ensure_budget`) · current as of 2026-11-14

## What it's for
Agents need to call Claude and DeepSeek, but an agent process must never hold a provider API key.
It runs code from the repository, and anything in its environment can leak. The gateway sits in
the middle:

```
agent process                      Gaia (trusted)                         provider
ANTHROPIC_BASE_URL=…/llm/anthropic  ┌──────────────────────────────┐
ANTHROPIC_API_KEY=gaia_rt_…   ───▶  │ /llm/{provider}/{path}       │
(a per-run token, not a key)        │ 1. resolve run token         │
                                    │ 2. allowlisted path + model  │  x-api-key: <real key>
                                    │ 3. ensure_budget  ──403──▶   │ ───────────────────────▶
                                    │ 4. strip headers, inject key │ ◀─────── stream ────────
                     ◀── same bytes │ 5. relay unchanged, meter    │
                                    │ 6. record llm_usage, charge  │
                                    └──────────────────────────────┘
```

This one choke point also enforces budgets: every model call passes through it, so it can refuse
the next call when a run or the day is over budget.

## Files
| File | Role |
|---|---|
| `llm/run_tokens.py` | `mint` / `resolve` / `revoke` / `revoke_for_run`. Only the token's SHA-256 is stored |
| `llm/gateway.py` | the router: auth, allowlists, budget, header rewrite, streaming relay, recording |
| `llm/metering.py` | pure parsers: a `UsageAccumulator` fed raw chunks (JSON or SSE), plus `estimate_usage` |
| `services/costs.py` | `ensure_budget` (run cap, then UTC-day cap), `BudgetExceeded`, peak-window pricing |
| `scripts/dev_run_token.py` | seeds a scratch run and prints a token, for manual testing |

## How it works

### Run tokens
`mint(session, workspace_id=…, run_id=…, provider="deepseek", ttl_s=…)` returns
`gaia_rt_<43 random chars>` **once**. The database keeps only `sha256(token)`, the provider it is
for, the expiry, and `revoked_at`. `resolve()` refuses a token in any of these cases:
- unknown, revoked or expired;
- its run is missing or finished (`completed`, `failed`, `cancelled`).

Runtimes (doc 10) mint one token per task and revoke it when the task ends.

Tokens are masked in logs and events **by shape**: the `gaia_rt_…` pattern in
`security/redaction.py`. They are not added to the redaction registry, which would grow forever.

### Two kinds of credential, never interchangeable
| Credential | Accepted on | Rejected on |
|---|---|---|
| admin `GAIA_API_TOKEN` (keychain) | `/api/v1/*` | `/llm/*`: it's just an unknown token there |
| run token `gaia_rt_…` | `/llm/*` (as `x-api-key` or `Authorization: Bearer`) | `/api/v1/*` |

### Allowlisted endpoints
- **Anthropic:** `v1/messages` and `v1/messages/count_tokens`.
- **DeepSeek:** `chat/completions`, `v1/chat/completions` and `anthropic/v1/messages` (DeepSeek's
  Anthropic-compatible endpoint, used by the doc 08 spike).

Anything else gets 404 **in the provider's own error shape**, so SDKs show a normal error. The
request's `model` must be in `prices.toml`, because the gateway refuses to forward a call it can't
price.

### Streaming and metering
The response is relayed **byte for byte** (`aiter_raw`); SSE included. While it passes through, the
`UsageAccumulator` parses a copy of each chunk:
- **Anthropic SSE:** input and cache tokens from `message_start`, output from the last
  `message_delta`.
- **DeepSeek:** the final chunk's `usage`. The gateway adds `stream_options.include_usage=true` to
  streaming requests so that chunk exists.

When the stream ends, the gateway records an `llm_usage` row and adds the cost to `run.cost_usd`
with one atomic `UPDATE … SET cost_usd = cost_usd + :c`, in a fresh session.

Two details matter here, and both were found in review:
- **Recording is shielded.** When the client disconnects, Starlette cancels the response through an
  anyio cancel scope, and without `anyio.CancelScope(shield=True)` the recording would be cancelled
  too, leaving the call free.
- **Missing usage is estimated high, not zero.** An interrupted DeepSeek stream never receives its
  usage chunk. `estimate_usage` charges request bytes ÷ 3 as full-price input tokens, and response
  bytes ÷ 3 as output tokens (`price_tier="estimated"`).

### Budgets
Before forwarding, `ensure_budget` raises `BudgetExceeded` when either is true:
- the run's `cost_usd` has reached its `budget_usd`;
- the workspace's `llm_usage` total for the current UTC day has reached `settings.daily_budget_usd`.

The gateway then answers **403** in the provider's shape. Harnesses treat 403 as final, unlike
429/5xx, which they retry. It also publishes a `run.budget_exceeded` event, and nothing goes
upstream.

**Known limit:** the check happens before a call, so N calls in flight at once can each pass and
together overshoot by up to N calls. Reserving budget up front is doc 18a's job.

### Prices
`prices.toml` holds USD per 1M tokens. DeepSeek's values were read from its pricing page on
2026-11-14. Peak hours are `peak_windows_utc = [[1, 4], [6, 10]]` (weekdays only), and off-peak is
half price. Chinese public holidays, which DeepSeek treats as off-peak, are priced at peak: a slight
over-count, which is the safe direction.

## Try it
```sh
GAIA_DATA_DIR=/tmp/g uv run gaia serve &
eval "$(GAIA_DATA_DIR=/tmp/g uv run python scripts/dev_run_token.py --provider deepseek \
        | head -1 | sed 's/^/RT=/')"
curl -s localhost:8765/llm/deepseek/chat/completions -H "Authorization: Bearer $RT" \
  -H 'content-type: application/json' \
  -d '{"model":"deepseek-v4-pro","messages":[{"role":"user","content":"hi"}]}'
```

## Changing it safely
- **A new endpoint** (a harness that needs one) goes in the allowlist, with a test. Never add a
  wildcard.
- **Never log** request or response bodies, or headers. One `llm.gateway` metadata line per call is
  the contract.
- **Never** forward a client header outside `FORWARDED_REQUEST_HEADERS`, or a client could smuggle
  credentials or `anthropic-beta` flags for endpoints we don't meter.
- **Keep the shield** in `_relay_response`'s `finally`. The test
  `test_a_client_disconnect_mid_stream_still_records_usage` fails without it.
- **A new model** needs a `prices.toml` entry first. The gateway answers 400 for unpriced models.

See also: [LLM clients & costs](llm.md) · [Python concepts → async](python-concepts.md#async-and-await) ·
[review 07b](../reviews/2026-11-14-review-07b.md).
