# Gaia backend: architecture overview

> **Audience:** anyone who needs the shape of the backend in ten minutes: the owner, a reviewer,
> or a developer on their first day. The detailed guides in
> [`docs/backend/`](../docs/backend/README.md) take each part further, with explainers for the
> Python techniques it uses.
>
> **Status:** living document, current as of implementation package 06 (2026-11-13). Update it
> whenever a package lands (see "Keeping this current" at the end).

## What the backend is

Gaia's backend is one Python process that runs on your own machine. It keeps track of
**repositories, tickets, agents, worktrees and runs**, exposes them over an authenticated HTTP API
to the web UI, and drives AI coding agents through a workflow that stops for human approval at every
irreversible step.

Today the foundation is built: configuration, secrets, the database, the API shell, and safe access
to git. The agent-facing parts (LLM clients, the sandbox, runtimes, the LangGraph workflow, GitHub)
come next, in the order given in [`docs/implementation/00-README.md`](../docs/implementation/00-README.md).

## The shape, in one picture

```
           Web UI (frontend/, shell built: doc 16)   gaia CLI (gaia/cli.py)
                     │  HTTP + bearer token                 │ secrets, serve, ui
                     ▼                                      ▼
┌──────────────────────────────────────────────────────────────────────────────┐
│ api/        FastAPI app: auth, middleware, error envelope, routes             │
│             (every /api/v1 route requires the token; scoped to a workspace)   │
├──────────────────────────────────────────────────────────────────────────────┤
│ orchestrator/  LangGraph workflow: plan → approve → implement → review → PR   │  built (11, 12a, 12b)
│ runtimes/      the agents themselves (Claude lead, DeepSeek juniors)          │  10a, 10b, 10c built
│ llm/           provider clients, cost accounting, LLM gateway (all built)     │  07a/07b/19a
│ tools/         sandboxed file/shell/git tools, registry, audit                │  built (09a, 09b)
├──────────────────────────────────────────────────────────────────────────────┤
│ services/   git, worktrees, tickets, costs, events, GitHub push/PR/merge      │
│             └─ proc.py: the ONLY place a process is ever started              │
├──────────────────────────────────────────────────────────────────────────────┤
│ db/ + domain/   SQLAlchemy models, migrations, workspace-scoped repositories, │
│                 enums, status state machines, Pydantic schemas                │
└──────────────────────────────────────────────────────────────────────────────┘
  cross-cutting:  config/ (settings, keychain secrets, logging)
                  security/ (redaction, path guard, command policy, srt sandbox profile)

  on disk:  ~/.gaia/ (mode 0700)  gaia.sqlite · checkpoints.sqlite · gitconfig · worktrees/
  in the OS keychain:  ANTHROPIC_API_KEY · DEEPSEEK_API_KEY · OPENAI_API_KEY · XAI_API_KEY
                       GITHUB_TOKEN · GAIA_API_TOKEN
```

Each layer depends only on the layers below it. `api/` calls `services/` and `db/`. `services/`
never imports FastAPI. `db/` knows nothing about HTTP or git.

## What exists today

| Package | Area | What it gives the rest of the system | Guide |
|---|---|---|---|
| 01 | Tooling | uv project, ruff + strict mypy, pytest (warnings are errors), gitleaks, CI | [tooling](../docs/backend/README.md#tooling-and-conventions) |
| 02 | `config/`, `security/redaction.py`, `cli.py` | Typed settings from `GAIA_*` env vars, keys in the macOS Keychain, redacted logs, `gaia secrets` | [config & secrets](../docs/backend/config-and-secrets.md) |
| 03 | `db/`, `domain/`, `alembic/` | 13 tables, migrations, workspace-scoped data access, status state machines | [database](../docs/backend/database.md) |
| 04 | `api/` | Localhost-only FastAPI app, bearer-token auth, uniform error envelope, `gaia serve` / `gaia ui` | [API](../docs/backend/api.md) |
| 05 | `services/proc.py`, `git.py`, `worktrees.py` | Safe process runner, hardened git, one git worktree per ticket | [git & processes](../docs/backend/git-and-processes.md) |
| 05b | `services/{git,proc,worktrees}.py`, migration `0002` | Worktree git pinned to the trusted admin dir (F1), bounded reap of escaped processes (F2), approval idempotency key (F4), commit-before-response (F6) | [git & processes](../docs/backend/git-and-processes.md) |
| 06 | `api/routes/`, `services/tickets.py`, migration `0003` | Repositories, tickets, agents and worktrees API with pagination, audit rows and `make openapi` | [API](../docs/backend/api.md) |
| 07a | `llm/`, `services/costs.py` | Claude and DeepSeek clients, priced and recorded in `llm_usage`; `FakeLLM` for tests | [LLM clients](../docs/backend/llm.md) |
| 07b | `llm/{gateway,run_tokens,metering}.py`, migration `0004` | Provider-key-free agents: per-run tokens, an allowlisted streaming proxy, metering, and budget hard-stop (403) | [gateway](../docs/backend/gateway.md) |
| 09a | `security/{paths,commands,sandbox}.py` | Path confinement, the command policy (a tripwire), and the `srt` sandbox profile (the boundary), proved live | [security core](../docs/backend/security.md) |
| 09b | `tools/`, `security/audit.py` | The one tool layer every runtime uses: confined file tools, two-mode search, `run_command` through policy → in-turn approval → `srt`, git tools, an audit row and events per call | [tools](../docs/backend/tools.md) |
| 15a | `services/{github,pull_requests}.py`, `GitService.push` | Token permission check, a push whose token lives only in one process's env, idempotent draft PRs, SHA-pinned squash merges after approval | [GitHub](../docs/backend/github.md) |
| 16 | `api/routes/{approvals,costs}.py`, `api/spa.py` | The approvals inbox list (for the nav badge), today's spend, and serving the built UI with its CSP. The UI itself: [frontend guide](../docs/frontend/README.md) | [API](../docs/backend/api.md) |
| 15b | `services/{ci,pr_sync}.py`, `api/routes/pull_requests.py`, migration `0005` | CI watching pinned to the approved commit, a background PR sync for changes made on GitHub, and the PR list API | [GitHub](../docs/backend/github.md) |
| 18b | `config/logging.py`, `api/routes/metrics.py` | Every log line (structlog and stdlib) redacted; rotating 0600 JSON log file under `data_dir/logs`; uvicorn access lines without query strings; metrics summary API | [config & secrets](../docs/backend/config-and-secrets.md) |
| 18a | `orchestrator/{guard,runner,pause}.py`, `api/routes/admin.py` | The daily cap pauses runs and blocks new ones; the kill switch (`pause-all` / `resume-all`) with a durable flag and a `paused` run state that startup never resumes | [orchestrator](../docs/backend/orchestrator.md) |
| 19a | `llm/openai_compat_client.py`, `llm/catalog.py`, `api/routes/models.py`, `prices.toml` | OpenAI and xAI as providers (keys, gateway routes, one OpenAI-compatible client with per-provider dialects); `prices.toml` as the model catalog (provider, label, tier, long-context tiers); `GET /models`; the gateway refuses a model on another provider's route | [LLM clients](../docs/backend/llm.md), [gateway](../docs/backend/gateway.md) |
| 19b | `domain/agent_modes.py`, `services/agent_modes.py`, `orchestrator/agent_mode_store.py`, `orchestrator/impl/{planning,review,steps}.py`, migration `0007` | Three agent modes per run: straight shot (one model, one session, no plan gate), two tiered (the lead assigns steps to a worker pool), wisdom of crowds (a panel plans in parallel, the lead merges). A saved default (`/admin/agent-mode`), a per-run override and snapshot (`run.agent_config`), and model → runtime routing by provider | [orchestrator](../docs/backend/orchestrator.md#agent-modes-19b) |
| auto mode | `orchestrator/auto_mode.py`, `api/routes/admin.py`, `ToolContext.auto_approve` | Per-model-family (Claude, DeepSeek, and since 19a OpenAI and xAI) auto-approval of `run_command` asks; a 0600 switch file in the data dir, read live; denies, the sandbox and protected-file asks unchanged | [tools](../docs/backend/tools.md) |
| allowed commands | `services/allowed_commands.py`, `security/commands.py` (`allowed_entry_problem`, `suggest_allowed_prefix`), migration `0006` | Per-repository allowed command prefixes in `extra_allow`, read live per command; suggestions from past approvals; **Approve + Log** (`remember: true`); every step's instructions list the repository's commands | [security core](../docs/backend/security.md) |
| 18b-2 | `dev/{fake_llm,demo}.py` | `GAIA_FAKE_LLM=1` demo mode (only the model is faked; no key, no network) and `gaia demo init` (a demo repository and ticket) | [gateway](../docs/backend/gateway.md#demo-mode-18b-2) |
| 12b | `orchestrator/impl/`, `orchestrator/wiring.py` | The real workflow: worktree and setup, steps on the configured runtimes, sandboxed checks, commits, lead planning and review through the gateway, one composition root in the app lifespan, and the swap drill (a full run with no Claude) | [orchestrator](../docs/backend/orchestrator.md) |
| 14 | `api/routes/runs.py`, `api/routes/approvals.py` (decide) | Start, list, inspect, cancel and diff runs; decide approvals (command answers wake the agent, gate decisions resume the graph; typed merge confirmation) | [API](../docs/backend/api.md) |
| 13 | `services/events.py`, `api/routes/stream.py` | Durable event log plus live SSE stream with single-use tickets, replay and gap healing | [events](../docs/backend/events.md) |
| 11 | `orchestrator/{lead,repo_context}.py`, `domain/plans.py` | The lead's plan and review calls: streamed structured outputs, policy-checked test commands, refusals escalate; the final review sees each step's latest checks ([fix](../docs/reviews/2026-11-17-final-review-checks.md)) | [lead](../docs/backend/lead.md) |
| 10a | `runtimes/`, `services/proc.py` (`stream_proc`) | The runtime seam: limits, scrubbed env, git-computed `files_changed`, in-turn approvals, orphan reaping, fake runtimes and a contract suite | [runtimes](../docs/backend/runtimes.md) |
| 10b | `runtimes/{native,native_llm,finish}.py`, `DeepSeekClient.parse` | Gaia's own provider-neutral agent loop (the default junior), through the gateway and the 09b tools; P1 vendor-boundary test; P6 lead planning on DeepSeek | [runtimes](../docs/backend/runtimes.md) |
| 10c | `runtimes/claude_agent_sdk.py` | Claude (via the Agent SDK) for lead work, as a thin adapter: Gaia's tools over in-process MCP, an `env -i` + `srt` wrapper, run token only, Gaia-priced usage | [runtimes](../docs/backend/runtimes.md) |
| 12a | `orchestrator/{graph,guard,runner,approvals}.py`, `nodes/` | The durable LangGraph ticket-run workflow on interfaces: idempotent human gates, budget guard, crash recovery, concurrency limit ([diagram](../docs/graph.md)) | [orchestrator](../docs/backend/orchestrator.md) |

The whole suite (about 1850 backend tests and 70+ frontend tests, 100% line and branch coverage) runs with `make check`, which is also what CI runs.

## Five design rules that explain most of the code

1. **Secrets live in the OS keychain, nowhere else.** Not in settings, `.env` files, logs, error
   messages or child processes. Any value read from the keychain is automatically masked in logs.
2. **Everything is scoped to a workspace.** There is one workspace today, but every table carries
   `workspace_id`, and the only way to query is through `ScopedRepo`, which filters on it. This is
   what lets Gaia become multi-user later without a rewrite.
3. **Repository content is untrusted.** Code, configs and hooks in a repo, or written by an agent,
   never configure Gaia. Git runs with hooks, signing and pagers neutralised, and child processes
   get a minimal, allowlisted environment.
4. **One door per dangerous capability.** One module starts processes (`services/proc.py`). One
   service talks to git (`services/git.py`). One function moves a status (`domain/state.transition`).
   One dependency gives routes database access (`api/deps.Scoped`). Tooling enforces the first of these.
5. **Local-first, cloud-ready.** It binds to `127.0.0.1` only, uses SQLite, and authenticates with a
   single token. Each of these sits behind an interface (`Settings`, `SecretStore`, `AuthProvider`,
   the DB URL), so Postgres, a cloud secret manager and OIDC are swaps rather than rewrites.

## How a request flows

1. The UI sends `GET /api/v1/...` with `Authorization: Bearer <token>`.
2. The middleware adds security headers, assigns a request id (also used in logs), and rejects
   unexpected `Host` headers. Rejecting those is the DNS-rebinding defence.
3. The router-level dependency checks the token against the keychain in constant time. The result
   is a `Principal`: who you are and which workspace you are in.
4. The route receives a `Scoped` object, which hands out workspace-filtered repositories, so it cannot
   read another workspace's rows.
5. Any failure leaves as `{"error": {"code", "message", "request_id"}}`, with the message redacted
   and no traceback.

## Known risks and open decisions

The 2026-11-13 code review ([`docs/reviews/2026-11-13-code-review-01-05.md`](../docs/reviews/2026-11-13-code-review-01-05.md))
found a **blocking** security issue (a planted `.git` file could make Gaia's own git execute code)
and the related F2, F4 and F6 issues. All four were fixed in package 05b
([review](../docs/reviews/2026-11-13-review-05b.md)). Still open: run-status transitions for doc 12 (F3),
cross-workspace foreign keys (F5), and the low-priority items. Architecture-level decisions are recorded in
[`docs/ARCHITECTURE.md`](../docs/ARCHITECTURE.md) and the plan review.

The first live end-to-end run ([live E2E 1](../docs/reviews/2026-11-14-live-e2e-1.md)) drove a
ticket through plan, sandboxed implementation, tests, review and commit with real providers, for
$0.068. Its four findings are fixed. **Open decision:** a repository without a GitHub remote
currently ends its run as `failed` at `open_pr`; the recommendation is `completed`, with the
branch left for a manual merge.

## Running it

```sh
make setup && make migrate     # once
make dev-backend               # API on http://127.0.0.1:8765
uv run --project backend gaia ui   # prints the login URL for the UI
make check                     # lint, typecheck, tests, secret scan
```

## Keeping this current

- When a package lands, update its row in "What exists today", move it out of "planned" in the
  picture, and add or extend its guide in `docs/backend/`.
- If code and these docs disagree, the code and the package's `docs/implementation/NN-*.md` are the
  source of truth. Fix the docs in the same PR.
