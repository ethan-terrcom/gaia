# Handoff: 18b-2 — `GAIA_FAKE_LLM=1` demo mode and `gaia demo init`

Filled from `HANDOFF_TEMPLATE.md` for the lead's sandboxed DeepSeek harness (2026-11-14). Your file
tools are confined to the worktree. Your `run` tool is sandboxed: no network, no git writes, no
installs. The lead commits and merges; your report goes through `finish`.

---

You are a junior engineer on **Gaia**, a local-first control plane for AI coding agents. Your task
is package **18b-2**: a demo mode in which the **whole system runs without spending tokens**, so a
first-time user (and the UI's browser tests) can watch a complete run.

**The design: fake only the model.** The LLM gateway forwards every provider request through one
`httpx.AsyncClient` (`AppServices.llm_http`, built in `gaia/api/app.py`'s lifespan). In demo mode
that client gets an `httpx.MockTransport` that answers like Anthropic and DeepSeek do. Everything
else stays real:
- run tokens, the gateway, usage metering and budgets;
- the lead's plan and review parsing;
- the junior's tool loop and Gaia's tools in the `srt` sandbox;
- the checks and the git commit.

## 1. Read first, in this order
1. `docs/implementation/18-budgets-observability-hardening.md` (18b) and
   `docs/implementation/10-agent-runtimes.md`, `07-llm-providers.md` (the gateway). Read the docs'
   "as built" sections, not only the plans.
2. `docs/implementation/00-README.md`, "Rules for every work package";
   `docs/decisions/ADR-001-agent-runtimes.md`, portability rules P1–P6.
3. `docs/backend/gateway.md`, `llm.md`, `runtimes.md`,
   `orchestrator.md`, `config-and-secrets.md`.
4. Code. **Read the request and response shapes from these files; do not guess them:**
   - `gaia/llm/gateway.py`: how it picks the upstream URL (`anthropic_base_url` /
     `junior_base_url`), how it reads usage from **streamed and non-streamed** responses for each
     provider (your fake must produce what it meters), and how a missing key becomes 503
     `provider_not_configured`;
   - `gaia/llm/anthropic_client.py`: `parse` is **always streamed** with `output_format`, so read
     how it consumes Anthropic's SSE events; `gaia/llm/deepseek_client.py`: chat completions,
     non-streaming `parse` in JSON mode, and how the tool loop calls it;
   - `gaia/runtimes/native.py` and `native_llm.py` (the junior loop, `reasoning_content`, tool
     calls), `gaia/runtimes/finish.py` (`FinishArgs`);
   - `gaia/orchestrator/lead.py` (`plan_ticket`, the reviews, `_denied_test_commands`: plan test
     commands must **start with** the repository's registered test command) and
     `gaia/orchestrator/impl/planning.py` (`lead_llm`);
   - `gaia/domain/plans.py` (`Plan`, `PlanStep`, `Review{verdict, feedback, issues}`);
   - `gaia/tools/registry.py` (`JUNIOR_TOOLS`, the `write_file` and `run_command` argument
     models);
   - `gaia/config/settings.py`, `gaia/api/app.py` (the lifespan), `gaia/orchestrator/wiring.py`,
     `gaia/cli.py` (the argparse patterns), `gaia/services/repositories.py` (or wherever
     repositories are registered: `POST /repositories`);
   - tests: `tests/test_gateway*.py` (how tests drive the gateway with a `MockTransport`),
     `tests/test_swap_drill.py`, `tests/test_cli*.py`.

## 2. Lead notes
These are design decisions already made. Follow them, and record any deviation in your report.

### 2.1 Settings
In one block, `# --- demo mode (18b-2) ---`:
- `fake_llm: bool = False`, read from `GAIA_FAKE_LLM`. Check how the other settings get their env
  prefix.
- When `fake_llm` is true, a `model_validator(mode="after")` **forces** `lead_runtime` and
  `junior_runtime` to `native_deepseek`. The Claude Agent SDK runtime launches the real `claude`
  CLI, which would spend real tokens. Read the role-to-runtime settings (ADR-001 P2) for the exact
  names, and log a warning that the override happened.
- `POST /runs` with `junior_runtime=claude_agent_sdk` while `fake_llm` → **422** with a clear
  message.

### 2.2 `gaia/dev/fake_llm.py`: `FakeProviderTransport`
An `httpx.AsyncBaseTransport`, or a handler for `httpx.MockTransport`, that:
- **Never touches the network** and ignores the `Authorization` / `x-api-key` header values. In
  demo mode the gateway still sends a placeholder key (§2.3).
- **Anthropic** (`POST {anthropic_base_url}/v1/messages`): answers with whatever the request asks
  for, streamed (`"stream": true`) or not, in Anthropic's exact format. For the **SSE stream**, send
  `message_start` (with `usage.input_tokens`), `content_block_start`, one or more
  `content_block_delta`s with `text_delta`, `content_block_stop`, `message_delta`
  (`stop_reason="end_turn"`, `usage.output_tokens`) and `message_stop`. Verify the event shapes
  against what `anthropic_client.py` and the gateway's meter read; the installed `anthropic` SDK's
  stream types are the reference.
- **DeepSeek** (`POST {junior_base_url}/chat/completions`): OpenAI-style chat completions,
  streamed or not, matching what `deepseek_client.py` and the gateway's meter read. Include
  `usage` with `prompt_tokens` and `completion_tokens`, and `reasoning_content` where the client
  expects it.
- **What it answers is decided from the request body:**
  - **A plan request** (the lead's plan parse; detect it from the schema or prompt, for example a
    JSON schema with `steps`): a valid `Plan` with **one** step whose title says what the demo
    does, for example "Add a demo note". Its `test_command` must satisfy `_denied_test_commands`:
    use `null` if that passes validation and the checks still run the repository's test command
    (read `lead.py` and the check node to confirm). Otherwise, extract the registered test command
    from the prompt, which `plan_ticket` now includes.
  - **A review request** (`verdict` in the schema): `{"verdict": "accept", "feedback": "Demo
    review: the change matches the step.", "issues": []}`.
  - **A junior tool-loop turn** (DeepSeek with `tools`), decided by **how many assistant turns are
    already in `messages`**:
    - turn 1: a `write_file` call creating `GAIA_DEMO.md`, with a short note naming the ticket
      title (take it from the first user message);
    - turn 2: a `run_command` call running the repository's test command, if the task text names
      one; otherwise skip to turn 3;
    - turn 3: a `finish` call with `FinishArgs` (`tests_passed: true` only if the previous tool
      result says the command passed, and `tests_run` filled in).
    - Echo the request's `model` in every response.
  - Anything unrecognised: a provider-style **400 error body**, so a mismatch fails loudly rather
    than hanging.
- Token counts are small fixed numbers (for example 800 in, 200 out), so the dashboard shows a
  small, non-zero cost through the real price table.

### 2.3 Wiring it in
- In the lifespan, when `settings.fake_llm`:
  - build `llm_http` with `transport=FakeProviderTransport()`;
  - log one **warning**, `gaia.demo_mode`, "GAIA_FAKE_LLM=1: no real model is called".
- **Provider keys:** in demo mode the gateway must not need, and must not read, real keys. Give
  the gateway one seam:
  - in `gateway.py`'s key lookup, `if settings.fake_llm:` use the placeholder
    `SecretStr("fake-llm-demo")` and **do not call the secret store**;
  - the placeholder is registered with redaction like any key;
  - this is the **only** edit allowed in `gaia/llm/`;
  - a test asserts that the secret store is not called in demo mode.
- `GET /api/v1/me` gains `demo_mode: bool`, so the UI can show a badge later. If `Principal` is
  also used internally, add a response model rather than a field on `Principal`.

### 2.4 `gaia demo init`
A CLI command that gives a first-time user something to run against:
- It creates `{data_dir}/demo/greet/`, a tiny git repo:
  - `greet.py` with `def greet(name): return f"Hello, {name}!"`;
  - `test_greet.py` using **stdlib `unittest`**: no install and no network, so it runs in the
    sandbox as is;
  - one commit on `main`, made with `GitService` and its pinned, safe config, as other code in
    `gaia/services` does.
- It registers the repo exactly as `POST /repositories` does, through the same service, with:
  - `test_command = ["python3", "-m", "unittest", "-q"]`;
  - no setup command.
- It creates one ticket: "Add a farewell() function", with a body asking for `farewell(name)` and
  a test.
- It is **idempotent**: run twice, it reuses the repo and does not duplicate the ticket.
- It prints what it made and the next step: "start `GAIA_FAKE_LLM=1 gaia serve`, open the UI, and
  press Start run on the ticket". It works with or without `GAIA_FAKE_LLM`.

### 2.5 Tests
- **Transport unit tests:**
  - each request kind yields the exact shape the real clients parse. Drive the **real**
    `AnthropicClient.parse` and `DeepSeekClient` against an `httpx.AsyncClient` with your
    transport, not a hand-written expectation;
  - streamed and non-streamed;
  - unknown requests → 400.
- **Gateway in demo mode:** a request through the real gateway route is metered (an `llm_usage`
  row with a non-zero cost) and never calls the secret store.
- **The headline test (end to end):** with `fake_llm=True` and a **real** git repo from
  `tests/git_repo.py`, drive a run through the **real** graph and wiring from `POST /runs` to the
  `pr_open` approval:
  - plan approval → approve;
  - the junior writes `GAIA_DEMO.md`;
  - the checks run;
  - the review accepts;
  - a commit exists on the run's branch containing `GAIA_DEMO.md`.

  Use the `command_runner` test seam (a host runner instead of `srt`, as other wiring tests do),
  because `srt` cannot nest in your sandbox. Mark a second copy with `skipif` unless
  `GAIA_SRT_PATH` is set; the lead runs it with the real sandbox.
- `gaia demo init`: creates the repo, the commit, the registration and the ticket; is idempotent;
  and the demo repo's test command passes on the host.
- **Settings:** the runtime override and its warning; the 422 for `claude_agent_sdk`.

## 3. Files
**You may create:**
- `gaia/dev/__init__.py`, `gaia/dev/fake_llm.py`, `gaia/dev/demo.py`;
- tests: `tests/test_fake_llm_transport.py`, `tests/test_demo_mode_e2e.py`,
  `tests/test_cli_demo.py`.

**You may edit:**
- `gaia/config/settings.py` (one block), `gaia/api/app.py` (the lifespan's `llm_http` only),
  `gaia/llm/gateway.py` (the key seam only);
- `gaia/api/routes/session.py` (`demo_mode`), `gaia/api/routes/runs.py` (the 422 only),
  `gaia/domain/schemas.py`, `gaia/cli.py` (the `demo` group);
- `tests/conftest.py` (`fake_llm=False` in the shared settings, if needed);
- `frontend/src/api/openapi.json` and `schema.d.ts` (regenerated only);
- `docs/implementation/18-budgets-observability-hardening.md`: add "Decisions and deviations (as
  built) — 18b-2" directly **under the 18b as-built section**.

**You must not touch:**
- `pyproject.toml`, `uv.lock`, `[tool.*]`;
- `.github/`, migrations;
- `gaia/runtimes/`, `gaia/tools/`, `gaia/orchestrator/` (except reading);
- `gaia/llm/` other than the gateway key seam;
- `frontend/src` other than the generated files;
- other docs.

Another junior is building 18a (budgets and the kill switch) in parallel. It edits `settings.py`,
`app.py`, `runs.py` and `wiring.py`. Keep your edits there small and in one block each.

## 4. Non-negotiable rules
- **Demo mode never sends a request off the machine and never reads a real key.** Prove both in
  tests.
- Demo mode is **off by default** and announced with a warning whenever it is on.
- The fake answers in the providers' real wire formats, checked against the real clients, so the
  demo exercises the real code paths.
- mypy strict; 100% coverage; no new `# type: ignore` or `# noqa` without a reason.

## 5. How to work
1. Read §1, then write a short plan as your first message.
2. Order:
   1. settings;
   2. the transport, tested against the real clients;
   3. the gateway seam;
   4. the lifespan;
   5. `/me`;
   6. `gaia demo init`;
   7. the end-to-end test;
   8. OpenAPI;
   9. the doc section.
3. Checks (from `backend/`): `uv run ruff check .`, `uv run ruff format --check .`, `uv run mypy`,
   `uv run pytest -q` (100% coverage). Frontend (`cwd: "frontend"`), after regenerating:
   `npm run lint`, `npm run typecheck`.

## 6. When blocked
Write the question under "Open questions", choose the most restrictive reasonable behaviour, and
keep going.

## 7. Report (pass to `finish`, in Markdown, with these headings)
Summary · Files changed · Deviations · Security-relevant changes · Tests added · Open questions ·
Final check output.
