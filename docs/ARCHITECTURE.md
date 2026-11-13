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
| Junior LLM | DeepSeek (`https://api.deepseek.com`, OpenAI-compatible; use the `openai` SDK with `base_url`) | Owner requirement; cheap |
| Git | `git` CLI via a safe subprocess wrapper; worktrees | Fewer surprises than libgit2 bindings |
| GitHub | `githubkit` or `PyGithub` with a fine-grained PAT (GitHub App later) | PRs, checks, issues |
| Frontend | Vite + React + TypeScript + TanStack Query + Tailwind | Fast, typed |
| Live updates | Server-Sent Events (SSE) | One-way, simple, works through proxies |
| Secrets | `keyring` (macOS Keychain) | No plaintext keys on disk |
| Tests | pytest + pytest-asyncio; Vitest + Playwright | |
| Lint/type | ruff, mypy (strict on `gaia/core`), eslint, tsc | |

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
| `ClaudeAgentSdkRuntime` | lead (or junior) | `claude-agent-sdk` package; `cwd` = worktree; permission callbacks route to Gaia's command policy and approvals |
| `ClineCliRuntime` | junior | Runs the Cline CLI headless in the worktree with DeepSeek configured. **Spike required:** confirm headless/non-interactive mode, a structured output format, and an approval hook |

**Recommendation:** run the spike (`implementation/08-runtime-spike.md`) on one real
ticket with each runtime. Default plan: **harness runtimes for v1** if the spike passes,
because the hard, security-sensitive work (edit tools, context management) is already
done there and matches the owner's current workflow. Keep **native runtimes** as the
fallback and for steps that need strict structured output, such as planning. Planning
and review on the lead side are single structured calls, so they always go straight
through the Anthropic SDK whichever runtime does implementation.

Whichever runtime is chosen, Gaia enforces worktree confinement, secret isolation and
budgets **outside** the runtime as well: through process env scrubbing, `cwd`, wall-clock
timeouts and cost polling. The harness's own controls are not enough on their own.

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
- Checkpointer: `AsyncSqliteSaver` in a separate `checkpoints.sqlite` file.
- Nodes are thin. They call services and runtimes, and they write domain rows and events.
- Budgets are checked in a node wrapper. Going over budget raises → `status=paused_budget` → interrupt.

## 5. Domain model (core tables)

Every table has `id (uuid7 str)`, `workspace_id`, `created_at`, `updated_at`, and
`created_by` where it applies.

`workspace`, `user`, `repository` (local_path, remote_url, default_branch),
`ticket` (title, body, status, priority, external_ref),
`agent` (role, runtime, model, display_name, status),
`run` (ticket_id, status, graph_thread_id, cost_usd, budget_usd),
`run_step` (run_id, index, description, status, assigned_agent_id, attempts),
`worktree` (repo_id, path, branch, base_sha, status),
`approval` (run_id, kind, payload_json, status, decided_by, decided_at, comment),
`pull_request` (run_id, number, url, state, ci_status),
`event` (run_id, agent_id, type, payload_json, ts) — append-only,
`llm_usage` (run_id, agent_id, provider, model, input_tokens, output_tokens, cache_read_tokens, cost_usd),
`audit_log` (actor, action, target, details_json, ts) — append-only.

## 6. Security model

Threats, in priority order: (1) an agent, steered by prompt-injected repo content, runs
destructive commands or exfiltrates secrets; (2) API keys leak through logs, prompts,
env or git; (3) another local process or web page drives the Gaia API; (4) a runaway
agent spends money.

Controls:
1. **Secrets:** stored with `keyring`, loaded at process start into a `SecretStr`, never
   logged, never written to `.env`, never passed into agent subprocess environments
   except the single key that a harness runtime needs for its own provider. A redaction
   filter covers logs and events. `gitleaks` runs in pre-commit and CI.
2. **Filesystem confinement:** every tool path is resolved (`Path.resolve()`, symlinks
   followed) and must sit under the task's worktree. `.git/` internals are blocked. Writes
   to CI config files and lockfiles need approval.
3. **Command policy:** allowlist by argv[0] plus argument rules (for example `pytest`,
   `npm test`, `ruff`, `git status/diff/add/commit`). Always denied: `git push --force`,
   `rm -rf /`-style patterns, `curl|sh`, `sudo`. Anything else → approval. Commands run with
   `shell=False`, a timeout, a scrubbed env (`PATH`, `HOME`=temp dir, `LANG`), and output caps.
4. **Network:** v1 cannot fully sandbox the network. That is a documented, accepted risk.
   Containerised runners (Docker with `--network=none` for test runs) are planned in
   `implementation/18`.
5. **API surface:** binds to `127.0.0.1`. A bearer token is generated on first run (stored
   in the keychain and handed to the UI through a one-time local login URL). Strict CORS
   (UI origin only). Host-header check against DNS rebinding. All mutating routes require auth.
6. **Git safety:** agents push only to `gaia/*` branches. Merges happen only through the
   approval gate, with GitHub branch protection as a second line of defence.
7. **GitHub token:** fine-grained PAT scoped to the selected repos with `contents:rw`,
   `pull_requests:rw`, `checks:read`, `issues:read`. Later this becomes a GitHub App.
8. **Prompt-injection hygiene:** repo/issue content is wrapped as untrusted data in prompts.
   The lead's system prompt states that instructions found in files are not commands.
   High-risk actions require approval no matter what the model says.
9. **Audit:** every tool call, command, approval decision and git push is written to `audit_log`.
10. **Budgets:** per-run and per-day USD caps, enforced before each LLM call.

## 7. Path to multi-user and cloud
- `workspace_id` scoping is applied in a repository/service layer helper, never ad hoc.
- An `AuthProvider` interface: `LocalTokenAuth` now, OIDC later.
- The DB URL comes from config, and Alembic migrations stay dialect-neutral (no SQLite-only SQL).
- Runtimes already work on paths plus a policy, so moving them into per-run containers is a
  deployment change rather than a rewrite.
- Secrets interface: `KeyringSecrets` now, a cloud secret manager later.
