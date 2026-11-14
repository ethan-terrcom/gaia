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
           Web UI (frontend/, doc 16)                gaia CLI (gaia/cli.py)
                     │  HTTP + bearer token                 │ secrets, serve, ui
                     ▼                                      ▼
┌──────────────────────────────────────────────────────────────────────────────┐
│ api/        FastAPI app: auth, middleware, error envelope, routes             │
│             (every /api/v1 route requires the token; scoped to a workspace)   │
├──────────────────────────────────────────────────────────────────────────────┤
│ orchestrator/  LangGraph workflow: plan → approve → implement → review → PR   │  planned (12)
│ runtimes/      the agents themselves (Claude lead, DeepSeek juniors)          │  planned (10)
│ llm/           provider clients + cost accounting (built), LLM gateway        │  07a / 07b
│ tools/         sandboxed file/shell tools for agents                          │  planned (09)
├──────────────────────────────────────────────────────────────────────────────┤
│ services/   git, worktrees, tickets, costs (built) · GitHub, events (planned) │
│             └─ proc.py: the ONLY place a process is ever started              │
├──────────────────────────────────────────────────────────────────────────────┤
│ db/ + domain/   SQLAlchemy models, migrations, workspace-scoped repositories, │
│                 enums, status state machines, Pydantic schemas                │
└──────────────────────────────────────────────────────────────────────────────┘
  cross-cutting:  config/ (settings, keychain secrets, logging)
                  security/ (redaction today; path guard, command policy, sandbox next)

  on disk:  ~/.gaia/ (mode 0700)  gaia.sqlite · checkpoints.sqlite · gitconfig · worktrees/
  in the OS keychain:  ANTHROPIC_API_KEY · DEEPSEEK_API_KEY · GITHUB_TOKEN · GAIA_API_TOKEN
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

The whole suite (495 tests, 100% line and branch coverage) runs with `make check`, which is also what CI runs.

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
