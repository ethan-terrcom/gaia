# 03 — Database & Domain Models

## Goal
Persist the domain model from `ARCHITECTURE.md §5` with migrations and workspace-scoped
data access.

## Out of scope
API routes (04/06) and LangGraph checkpoint tables (LangGraph manages those itself, in a separate file).

## Files
- `gaia/db/engine.py`: async engine and session factory. On SQLite, run `PRAGMA journal_mode=WAL`
  and `PRAGMA foreign_keys=ON` on connect.
- `gaia/db/ids.py`: the UUIDv7 helper (task 1 allows "a small helper" instead of `uuid6`).
- `gaia/db/types.py`: `UtcDateTime`, so timestamps read back timezone-aware on SQLite.
- `gaia/db/models.py`: SQLAlchemy 2.x declarative models (`Mapped[...]`).
- `gaia/db/repo.py`: the scoped repository helper.
- `gaia/db/seed.py`: the default workspace and local user (task 7).
- `gaia/domain/enums.py`, `gaia/domain/schemas.py`: Pydantic read/create schemas.
- `backend/alembic/`: migrations (`alembic.ini`, `env.py`, `script.py.mako`, `versions/`).

## Tasks
1. A base mixin: `id: str` (UUIDv7 as a string, from the `uuid6` package or a small helper),
   `workspace_id`, `created_at`, `updated_at` (UTC, timezone-aware).
2. Models for every table in ARCHITECTURE §5, including the fields the 2026-11-12 review added
   (`ticket.repo_id`, `event.seq`, and the extra `run`, `run_step`, `llm_usage` and `repository`
   columns). The user table is `users`, because `user` is reserved in Postgres. `event.seq` is an
   autoincrementing integer, unique, and the SSE event ID (doc 13). Use `JSON` columns for
   payloads, not SQLite-specific types. Add indexes on `(workspace_id, status)` for `run` and `ticket`,
   and on `(run_id, seq)` for `event`.
3. Enums: `TicketStatus` (backlog, ready, in_progress, in_review, done, cancelled), `RunStatus`
   (pending, planning, awaiting_approval, implementing, reviewing, pr_open, paused_budget, failed,
   completed, cancelled), `ApprovalKind` (plan, command, escalation, pr_open, merge, budget), `ApprovalStatus`,
   `AgentRole` (lead, junior), `RuntimeKind` (native_claude, native_deepseek, claude_agent_sdk, cline_cli).
4. Allowed status transitions live in `domain/state.py` as a dict. Write a
   `transition(entity, new_status)` that raises on an illegal move.
5. `ScopedRepo[T]`: `get`, `list`, `create`, `update`. It **always** filters by the `workspace_id`
   it was constructed with. Services must use it; no raw `select()` on tenant tables outside `db/`.
6. `event` and `audit_log` repos expose only `append` and `list`. No update or delete.
7. Seed on first start: one default workspace and one local user.
8. Initial Alembic migration. `make migrate` target.

## Tests
- Every model round-trips create and read.
- `ScopedRepo` cannot read another workspace's row (write a test with two workspaces).
- An illegal status transition raises.
- The migration runs on an empty DB, and `alembic check` shows no drift.

## Acceptance criteria
- [x] `make migrate` creates the schema in `~/.gaia/gaia.sqlite`.
- [x] Tests pass. mypy is strict-clean for `gaia/db` and `gaia/domain`.

## Decisions and deviations (as built)

**Status machines (task 4).** The doc names the file and `transition()` but not the edges, so
`domain/state.py` derives them from the graph in ARCHITECTURE §4:

| Row | Rules |
|---|---|
| `ticket` | `backlog → ready`, `ready → in_progress`, `in_progress → in_review`, `in_review → done`, each also `→ cancelled`, and `in_review → in_progress` when review sends it back. `done` and `cancelled` are terminal. |
| `run` | `pending → planning`, `planning → awaiting_approval`/`implementing`, `implementing ↔ reviewing`, `reviewing → pr_open`, `pr_open → completed`, any live status `→ paused_budget`/`failed`/`cancelled`, `paused_budget →` any of planning/implementing/reviewing/awaiting_approval. `awaiting_approval` is shared by every gate, because which gate is open lives on the `approval` row. |
| `run_step` | `pending → running`, `running → completed`/`failed`, `failed → pending` (a rejected step is retried, doc 12), `completed`/`cancelled` terminal. |

A move to the current status is a no-op, not an error: nodes write their rows idempotently, and a
resumed run re-applies a status it already set. Doc 12 may refine these edges; `transition()` is
the only place to change them.

**Deviations from the text of this doc and ARCHITECTURE §5.**

1. **Enums are text columns** (`String(16)`/`String(32)`), not dialect enum types. A PostgreSQL
   enum needs a `ALTER TYPE` per new value and SQLite has none, so the value is validated at the
   two boundaries where rows are written: the Pydantic schemas and `transition()`.
2. **`workspace` has no `workspace_id`.** §5 says every table has one, but the workspace *is* the
   tenant; `users` and every other table do have it.
3. **`run_step.step_index`** rather than `index`, which is reserved in SQLite and PostgreSQL.
4. **`event.seq` is the primary key**, with `id` (UUIDv7) as a unique column. SQLite can only
   autoincrement an `INTEGER PRIMARY KEY`, so this is what makes `seq` database-assigned and
   monotonic for SSE `Last-Event-ID` replay (2026-11-12 review §4). Keeping `id` unique preserves
   client-side deduplication.
5. **Extra enums** for columns §5 names without values: `AgentStatus`, `RunStepStatus`,
   `WorktreeStatus`, `PullRequestState`, `CiStatus`, plus `ApprovalStatus`.
   `ApprovalStatus` is `pending`/`approved`/`rejected`/`expired` (`expired` is the default-deny
   timeout of an in-turn command approval).
6. **Uniqueness the schema now enforces**: `workspace.slug`, `users (workspace_id, email)`,
   `repository (workspace_id, name)`, `ticket (repo_id, external_ref)`,
   `worktree (repo_id, branch)`, `run_step (run_id, step_index)`,
   `pull_request (run_id, number)`.
7. **`created_by`** is on `repository`, `ticket` and `run` ("where it applies"); `approval` has
   `decided_by`, and `audit_log.actor` is free text because a tool call's actor may be an agent
   with no `users` row.
8. **Money is `Numeric(12, 6)`** and returns `Decimal` on SQLite as well as PostgreSQL; no `float`
   touches a cost.
9. **New dependency facet:** `sqlalchemy[asyncio]`, which the async engine requires (`greenlet`).
   No package the doc does not name was added.
10. **`make migrate` is a new Makefile target** (task 8 asks for it). Alembic reads
    `Settings.db_url` - there is no URL in `alembic.ini` - and treats `data_dir` as `0o700` when
    it is the one that creates `~/.gaia`.
11. **Alembic runs synchronously** against the same file: `sync_url()` drops `+aiosqlite`, so
    `sqlite3` opens it.
12. **`ScopedRepo` has no `delete`**: the doc lists `get`/`list`/`create`/`update`, and deletion is
    a cascade from the workspace (or the parent row).
