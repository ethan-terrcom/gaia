# Handoff: 12b-2 — Real lead services for the graph (planner, reviewer) through the gateway

Filled from `HANDOFF_TEMPLATE.md` for the lead's sandboxed DeepSeek harness (2026-11-14). Your file
tools are confined to the worktree. Your `run` tool is sandboxed: no network, no git writes, no
installs. The lead commits and merges; you never commit. Your report goes through `finish`.

---

You are a junior engineer on **Gaia**, a local-first control plane for AI coding agents. The
orchestrator graph (12a) runs on protocols, and the lead's planning and review calls exist as
`LeadEngineer` (11). Your package, **12b-2**, writes the **real `Planner` and `Reviewer`**:
- the lead's calls go **through Gaia's LLM gateway** with a per-run token, so budgets and metering
  apply exactly as they do for agents;
- the **provider is a setting**: Claude by default, DeepSeek as the tested alternative. This is
  ADR-001 **P2** and **P6** for the lead side.

The execution side (worktree, steps, checks, commits) is **12b-1**, built in parallel by another
junior, and the app wiring is 12b-3. Build neither.

## 1. Read first, in this order
1. `docs/decisions/ADR-001-agent-runtimes.md`: **Portability P1–P6**, and Consequences 4–5
   (stream long lead calls; the gateway is the cost truth).
2. `docs/implementation/11-lead-planning-and-review.md` (whole, including the lead review
   changes) and `docs/implementation/12-orchestrator-graph.md` (the 12a decisions).
3. `docs/implementation/00-README.md`, "Rules for every work package".
4. `docs/backend/lead.md`, `llm.md`, `gateway.md`, `orchestrator.md`.
5. `docs/reviews/2026-11-14-review-11.md`, `…-review-07b.md`, `…-review-10b.md`.
6. Code:
   - `gaia/orchestrator/{lead,repo_context,deps,state}.py` and `nodes/{lead_plan,lead_review_step,
     lead_final_review,approve_plan}.py`;
   - `gaia/llm/{base,anthropic_client,deepseek_client,run_tokens,gateway,untrusted}.py`;
   - `gaia/runtimes/native_llm.py` (10b's `gateway_llm_factory` and `UsageTally`; reuse them);
   - `gaia/services/{costs,git,worktrees}.py`, `gaia/domain/plans.py`, `gaia/db/{models,repo}.py`,
     `gaia/config/settings.py`;
   - `tests/test_lead*.py`, `tests/test_lead_portability.py` (10b), and the gateway tests
     (in-process gateway with an upstream `MockTransport`).

## 2. Lead notes for this package
These are design decisions already made. Follow them, and record any deviation in your report.

### 2.1 Settings (P2)
In `gaia/config/settings.py`, add one block:
- `planner_provider: Literal["anthropic", "deepseek"] = "anthropic"`;
- `planner_model: str = "claude-opus-5-5"`;
- `lead_call_timeout_s: float = 600`.

12b-1 adds the runtime settings in a separate block. Do not add those.

### 2.2 A provider-neutral factory: `gaia/llm/providers.py`
`def build_structured_llm(*, provider, model, api_key: SecretStr, base_url: str, recorder,
price_table) -> StructuredLLM` dispatches to the adapter classes. It imports
`gaia.llm.anthropic_client` and `gaia.llm.deepseek_client`, **never `anthropic` or `openai`
themselves** (P1; 10b's `tests/test_vendor_boundaries.py` enforces it).
- **`AnthropicClient` needs a `base_url`.** Add `base_url: str | None = None` to its constructor
  and pass it to `anthropic.AsyncAnthropic(api_key=..., base_url=...)`. Check that constructor
  parameter in the installed SDK at `backend/.venv/lib/python3.12/site-packages/anthropic/
  _client.py`. The gateway route is `http://<host>:<port>/llm/anthropic`; the SDK appends
  `/v1/messages`. Check the gateway's allowlist in `gaia/llm/gateway.py` to confirm the path it
  accepts.
- `DeepSeekClient` already takes `base_url`. Its gateway route is
  `http://<host>:<port>/llm/deepseek`.

### 2.3 Per-call LLM access: `gaia/orchestrator/impl/lead_llm.py`
An async context manager:
```python
@asynccontextmanager
async def lead_llm(*, settings, sessions, price_table, run_id, workspace_id) -> AsyncIterator[StructuredLLM]
```
1. Mint a run token in its own transaction:
   `run_tokens.mint(..., provider=settings.planner_provider, ttl_s=int(settings.lead_call_timeout_s) + 300, run_step_id=None, agent_id=None)`.
2. Build the client with `build_structured_llm(...)`, the gateway base URL, and `UsageTally` as
   the recorder. **The gateway writes the `llm_usage` row and adds the run's cost**, so the
   client must not write a second row (P4). Reuse 10b's `UsageTally` from
   `gaia/runtimes/native_llm.py`; if its import path differs, read it.
3. In `finally`: revoke the token and close the client (`aclose`, if the adapter has one; add it
   to `AnthropicClient` if missing).

### 2.4 `impl/planning.py`: `GatewayPlanner` (implements `Planner.plan(run_id) -> Plan`)
1. **Load** (short session): run, ticket, repository and worktree location. Build
   `TicketInput(id, title, body=ticket.description or "")`; check the ticket model's body field
   name.
2. `repo_context = await build_repo_context(<worktree root>, ticket_input, ...)`. Read its
   signature.
3. `async with lead_llm(...) as llm:` run `LeadEngineer(llm, policy).plan_ticket(ticket_input,
   repo_context, extra_allow=(repo.test_command,) if set)`.
4. `LeadEscalation` and `PlanRejected` propagate unchanged; the guard turns them into a failed run
   with the message, and the node already handles the rest.
5. **No DB session is open during the LLM call.**

### 2.5 `impl/review.py`: `GatewayReviewer` (implements `Reviewer`)
- **The plan** is rebuilt from the DB, because the protocol gives only `run_id`:
  - `steps` come from the `run_step` rows ordered by `step_index` (`PlanStep.model_validate
    (row.spec_json)`); these reflect an edited plan;
  - `summary` and `risks` come from the most recent `plan` approval's `payload_json["plan"]`
    (approved or pending), falling back to `""` and `[]`.

  Put this in a pure helper, `plan_from_rows(steps, approval_payload) -> Plan`, and test it.
- `review_step(run_id, step_index, outcome, checks)`: `LeadEngineer.review_step(plan, step_index,
  outcome.diff, checks.output)`, inside `lead_llm`.
- `final_review(run_id)`: full diff `GitService.diff(location, worktree.base_sha, "HEAD")` (read
  its signature; the worktree row has `base_sha`), then `LeadEngineer.final_review(plan, diff)`.
  `LeadEngineer` truncates large diffs itself.

### 2.6 P5/P6: prove the lead never assumes Claude
`tests/test_lead_services_portability.py` runs `GatewayPlanner.plan` and both reviewer methods
**twice**, once with `planner_provider="anthropic"` and once with `"deepseek"`.
- Each goes through an **in-process gateway** (the app's gateway router with
  `llm_transport=httpx.MockTransport(...)` faking the upstream provider), returning a valid
  structured response in that provider's wire format:
  - Anthropic: a streamed `messages` response with a tool or JSON block, matching how
    `AnthropicClient.parse` calls it. Read 11's tests for the exact shape they fake;
  - DeepSeek: `chat.completions` with `response_format=json_object`, matching 10b's
    `DeepSeekClient.parse`.
- Assert, for both:
  - a valid `Plan` or `Review` comes back;
  - **exactly one `llm_usage` row per call**, written by the gateway;
  - `run.cost_usd` increased;
  - the run token was revoked;
  - no provider key reached the client: the gateway holds it, in an `InMemorySecretStore`.

If wiring a real in-process gateway with the SDK clients proves too hard (the SDKs need an
`http_client` pointed at `httpx.ASGITransport(app)`), say so in "Open questions". Then fall back
to stub clients for the portability test, and keep one gateway-path test for DeepSeek only. Do
not spend more than about 30 turns on this.

## 3. Files
**You may create:**
- `gaia/llm/providers.py`, `gaia/orchestrator/impl/{lead_llm,planning,review}.py`. If
  `gaia/orchestrator/impl/__init__.py` does not exist on your branch, create it with only a
  docstring; 12b-1 also creates it, and the lead merges;
- tests: `tests/test_llm_providers.py`, `tests/test_impl_planning.py`, `tests/test_impl_review.py`,
  `tests/test_lead_services_portability.py`.

**You may edit:**
- `gaia/llm/anthropic_client.py` (`base_url`, `aclose`), `gaia/config/settings.py` (2.1 only),
  and the related existing tests;
- `docs/implementation/12-orchestrator-graph.md`: add "Decisions and deviations (as built) —
  12b-2".

**You must not touch:**
- `pyproject.toml`, `uv.lock`, `[tool.*]`;
- `.github/`, migrations;
- `gaia/orchestrator/{deps,graph,guard,runner,lead,repo_context}.py` and `nodes/`;
- `gaia/runtimes/`, `gaia/tools/`, `gaia/api/`;
- other docs.

## 4. Non-negotiable rules
- **P1:** `anthropic` only in `anthropic_client.py`, `openai` only in `deepseek_client.py`.
- **P2:** `gaia/orchestrator/` never names a vendor or a model; read settings.
- **P4:** the gateway is the only writer of `llm_usage`. No double counting.
- No DB session held across an LLM call.
- Keys: the lead's calls carry a run token only. No provider key in any client constructor except
  inside the gateway process's own provider calls (07b).
- mypy strict; 100% coverage; no new `# type: ignore` or `# noqa` without a reason.

## 5. How to work
1. Read §1, then write a short plan as your first message.
2. Order: settings → `AnthropicClient.base_url`/`aclose` → `providers.py` → `lead_llm` → planner →
   `plan_from_rows` and the reviewer → the portability test.
3. Checks (from `backend/`): `uv run ruff check .`, `uv run ruff format --check .`,
   `uv run mypy`, `uv run pytest -q` (100% coverage).

## 6. When blocked
Write the question under "Open questions", choose the most restrictive reasonable behaviour, and
keep going.

## 7. Report (pass to `finish`, in Markdown, with these headings)
Summary · Files changed · Deviations · External APIs used · Security-relevant changes ·
Portability (P1–P6) · Tests added · Open questions · Final check output.
