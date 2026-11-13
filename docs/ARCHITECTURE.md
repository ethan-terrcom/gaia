# Gaia — Architecture

Read `PROJECT_BRIEF.md` first. This document records the decisions; the
`implementation/` docs carry them out.

## 1. Stack

| Concern | Choice | Why |
|---|---|---|
| Backend language | Python 3.12, managed with `uv` | LangGraph, the Anthropic SDK and the Claude Agent SDK are Python-first |
| API | FastAPI + Uvicorn | Async, typed, OpenAPI for free |
| Orchestration | LangGraph (`langgraph`) + `langgraph-checkpoint-sqlite` (later `-postgres`) | Durable state machine, `interrupt()` for approvals, resumable after crashes |
| DB | SQLAlchemy 2.x (async) + Alembic; SQLite in WAL mode locally, Postgres later | Zero-ops locally, clear path to the cloud |
| Lead LLM | Anthropic Python SDK, model `claude-opus-5-5` | Owner requirement |
| Junior LLM | DeepSeek `deepseek-v4-pro` or `deepseek-flash` (`https://api.deepseek.com`, OpenAI-compatible via the `openai` SDK; an Anthropic-compatible endpoint is also served at `/anthropic`) | Owner requirement; cheap. ADR-001 confirms the model |
| Git | `git` CLI via a safe subprocess wrapper; worktrees | Fewer surprises than libgit2 bindings |
| GitHub | `githubkit` or `PyGithub` with a fine-grained PAT (GitHub App later) | PRs, checks, issues |
| Frontend | Vite 8 + React 19 + TypeScript 7 + TanStack Query + Tailwind; Node 26 LTS | Fast, typed |
| Live updates | Server-Sent Events (SSE) | One-way, simple, works through proxies |
| Secrets | `keyring` (macOS Keychain) | No plaintext keys on disk |
| Tests | pytest + pytest-asyncio; Vitest + Playwright | |
| Lint/type | ruff, mypy (strict on all of `gaia`), oxlint + prettier, tsc (strict) | oxlint is the create-vite default |

## 2. Repository layout

```
gaia/
  backend/
    pyproject.toml
    gaia/
      config/          # settings, secrets (keyring)
      db/              # engine, models, alembic
      domain/          # pydantic schemas, enums, state machines
      services/        # git, worktrees, tickets, github, events, costs
      llm/             # provider clients (anthropic, deepseek), cost accounting
      runtimes/        # AgentRuntime interface + implementations (§3)
      tools/           # sandboxed tools for native runtimes
      orchestrator/    # LangGraph graphs, nodes, checkpointer
      api/             # FastAPI routers, auth, SSE
      security/        # path guards, command policy, redaction, audit
    tests/
  frontend/
  docs/
```

## 3. Agent execution: the `AgentRuntime` seam

The owner is open to either building agents on the raw APIs (**native**) or driving
existing harnesses such as Claude Code and Cline (**harness**). Gaia must not hard-code
either choice. Every agent turn goes through one interface:

```python
class AgentRuntime(Protocol):
    name: str
    async def run_task(self, task: AgentTask, events: EventSink) -> AgentResult: ...
    async def cancel(self, task_id: str) -> None: ...
```

- `AgentTask` holds `role` (lead|junior), `instructions`, `worktree_path`, `allowed_tools`,
  `command_policy`, `budget`, `timeout_s`, and `output_schema` (optional).
- `AgentResult` holds `status`, `summary`, `structured_output`, `files_changed`, `usage`
  (tokens/cost), and `transcript_ref`.
- `EventSink` streams progress (tool calls, messages, cost) to the event bus.

Implementations:

| Runtime | Role | Notes |
|---|---|---|
| `NativeClaudeRuntime` | lead | Anthropic SDK tool loop over Gaia's sandboxed tools (§6) |
| `NativeDeepSeekRuntime` | junior | OpenAI SDK pointed at DeepSeek; same tools |
| `ClaudeAgentSdkRuntime` | lead (or junior) | `claude-agent-sdk` package; `cwd` = worktree; `setting_sources=[]` so the worktree's `.claude/` settings, hooks and `CLAUDE.md` never load; unwanted tools removed with `disallowed_tools` (`allowed_tools` only auto-approves, it does not restrict); a `PreToolUse` hook routes every call to Gaia's command policy and approvals. Can also drive DeepSeek through its Anthropic-compatible endpoint (a spike candidate) |
| `ClineCliRuntime` | junior | Runs the Cline CLI headless in the worktree with DeepSeek configured. Headless mode and JSON-lines output exist, but it **auto-approves by default**, loads hooks from the workspace's `.cline/hooks`, and `-k` persists the key to disk. **Spike required:** confirm `PreToolUse` denial works headless, workspace hooks can be disabled, and the key can come from the gateway |

**Recommendation:** run the spike (`implementation/08-runtime-spike.md`) on one real
ticket with each runtime. Working hypothesis, which ADR-001 confirms or overturns: **harness runtimes for v1** if the spike passes,
because the hard, security-sensitive work (edit tools, context management) is already
done there and matches the owner's current workflow. Keep **native runtimes** as the
fallback and for steps that need strict structured output, such as planning. Planning
and review on the lead side are single structured calls, so they always go straight
through the Anthropic SDK whichever runtime does implementation.

Whichever runtime is chosen, Gaia enforces worktree confinement, secret isolation and budgets
**outside** the runtime as well: an OS sandbox around every agent process (§6.4), the LLM
gateway (provider keys never enter an agent's environment, and budgets stop at the gateway;
§6.1), process env scrubbing, `cwd` and wall-clock timeouts. The harness's own controls are
not enough on their own. (Sandbox and gateway are pending owner decisions D1 and D2 in
`reviews/2026-11-12-plan-review.md`.)

## 4. Orchestration (LangGraph)

One graph per ticket run (`thread_id = run_id`). State (`RunState`, a TypedDict):
`run_id, ticket_id, repo_id, worktree_id, plan, step_index, step_results, review,
pr_number, budget_used, status, error`.

```
START → prepare_worktree → lead_plan → [interrupt: approve_plan]
      → implement_step (junior) → run_checks → lead_review_step
          ├─ "revise" → implement_step (max N retries per step, then escalate to human)
          └─ "accept" → next step or → lead_final_review
      → [interrupt: approve_pr] → open_pr → watch_ci → [interrupt: approve_merge] → merge → END
```

- Human gates use `langgraph.types.interrupt(payload)`. The API resumes with
  `graph.ainvoke(Command(resume=decision), config)`.
- Graph interrupts are only for gates **between** agent turns: `plan`, `escalation`, `budget`,
  `pr_open`, `merge`. A command approval *inside* an agent turn is awaited by the runtime's
  permission hook while the session stays alive. A node re-runs from its first line on resume,
  so interrupting mid-turn would discard and re-pay the whole junior session.
- Every side effect before an `interrupt()` is an idempotent upsert keyed by
  `(run_id, node, step_index, attempt)`.
- Durability is set explicitly (`durability="sync"`). A crashed run is resumed by invoking the
  graph with input `None` on the same `thread_id`.
- The checkpoint owns the workflow position. `run` and `run_step` rows are projections that
  nodes write idempotently and startup reconciles.
- Checkpointer: `AsyncSqliteSaver` in a separate `checkpoints.sqlite` file.
- Nodes are thin. They call services and runtimes, and they write domain rows and events.
- Budgets are checked in a node wrapper. Going over budget raises → `status=paused_budget` → interrupt.

## 5. Domain model (core tables)

Every table has `id (uuid7 str)`, `workspace_id`, `created_at`, `updated_at`, and
`created_by` where it applies.

`workspace`, `users` (not `user`: reserved in Postgres), `repository` (local_path, remote_url,
default_branch, setup_command, test_command; both commands owner-registered, never read from the repo),
`ticket` (repo_id, title, body, status, priority, external_ref),
`agent` (role, runtime, model, display_name, status),
`run` (ticket_id, repo_id, worktree_id, junior_runtime, status, graph_thread_id, cost_usd,
budget_usd, started_at, finished_at),
`run_step` (run_id, index, title, spec_json, status, assigned_agent_id, attempts, result_summary),
`worktree` (repo_id, path, branch, base_sha, status),
`approval` (run_id, kind, payload_json, status, decided_by, decided_at, comment),
`pull_request` (run_id, number, url, state, ci_status),
`event` (seq, run_id, agent_id, type, payload_json, ts) — append-only; `seq` is a monotonic
integer used as the SSE event ID,
`llm_usage` (run_id, run_step_id, agent_id, provider, model, request_id, input_tokens, output_tokens,
cache_read_tokens, cache_creation_tokens, price_tier, cost_usd),
`audit_log` (actor, action, target, details_json, ts) — append-only.

## 6. Security model

Threats, in priority order: (1) an agent, steered by prompt-injected repo content, runs
destructive commands or exfiltrates secrets; (2) API keys leak through logs, prompts,
env or git; (3) another local process or web page drives the Gaia API; (4) a runaway
agent spends money.

Controls:
1. **Secrets:** stored with `keyring`, loaded at process start into a `SecretStr`, never
   logged, never written to `.env`, and never passed into agent subprocess environments.
   Runtimes reach their provider through the Gaia LLM gateway with a per-run token (pending
   D2): a harness holding the real key would expose it to every shell command it runs. A
   redaction filter covers logs and events. `gitleaks` runs in pre-commit and CI.
2. **Filesystem confinement:** every tool path is resolved (`Path.resolve()`, symlinks
   followed) and must sit under the task's worktree. `.git/` internals are blocked. Writes
   to CI config files and lockfiles need approval.
3. **Command policy:** allowlist by argv[0] plus argument rules (for example `pytest`,
   `npm test`, `ruff`, `git status/diff/add/commit`). Always denied: `git push --force`,
   `rm -rf /`-style patterns, `curl|sh`, `sudo`. Anything else → approval. Commands run with
   `shell=False`, a timeout, a scrubbed env (`PATH`, `HOME`=temp dir, `LANG`), and output caps.
   The policy is a tripwire, not a boundary: allowlisted test runners execute code the agent
   wrote, so containment comes from the sandbox (point 4).
4. **OS sandbox (pending D1):** every agent-spawned process (harness, test run, native
   `run_command`) runs under `sandbox-runtime` (Seatbelt on macOS). Writes are limited to the
   worktree; `~/.ssh`, `~/.aws`, `~/.config` and the keychain are unreadable; the network is
   limited to a per-repo allowlist plus the gateway. If D1 defers this, the accepted risk is
   that an agent can read and send anything the owner can, not just reach the network. Docker
   runners (`implementation/18c`) remain the stronger option.
5. **API surface:** binds to `127.0.0.1`. A bearer token is generated on first run (stored
   in the keychain and handed to the UI through a one-time local login URL). The UI is served
   from the same origin as the API, so there is no CORS allowance (docs 04, 16). Host-header
   check against DNS rebinding. All mutating routes require auth.
6. **Git safety:** agents push only to `gaia/*` branches. Merges happen only through the
   approval gate, with GitHub branch protection as a second line of defence.
7. **GitHub token:** fine-grained PAT scoped to the selected repos with `contents:rw`,
   `pull_requests:rw`, `checks:read`, `issues:read`. Later this becomes a GitHub App.
8. **Prompt-injection hygiene:** repo/issue content is wrapped as untrusted data in prompts.
   The lead's system prompt states that instructions found in files are not commands.
   High-risk actions require approval no matter what the model says.
9. **Audit:** every tool call, command, approval decision and git push is written to `audit_log`.
10. **Budgets:** per-run and per-day USD caps, enforced before each LLM call (at the gateway
    when D2 is adopted).
11. **Repo content never configures Gaia's tools:** harnesses run with project settings and
    hooks disabled (Agent SDK `setting_sources=[]`; Cline hooks only from a Gaia-owned dir).
    Gaia's git calls disable repo hooks (`core.hooksPath=/dev/null`, `commit --no-verify`) and use
    a Gaia-managed git config.

## 7. Path to multi-user and cloud
- `workspace_id` scoping is applied in a repository/service layer helper, never ad hoc.
- An `AuthProvider` interface: `LocalTokenAuth` now, OIDC later.
- The DB URL comes from config, and Alembic migrations stay dialect-neutral (no SQLite-only SQL).
- Runtimes already work on paths plus a policy, so moving them into per-run containers is a
  deployment change rather than a rewrite.
- Secrets interface: `KeyringSecrets` now, a cloud secret manager later.
