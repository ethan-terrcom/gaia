# 03 — Database & Domain Models

## Goal
Persist the domain model from `ARCHITECTURE.md §5` with migrations and workspace-scoped
data access.

## Out of scope
API routes (04/06) and LangGraph checkpoint tables (LangGraph manages those itself, in a separate file).

## Files
- `gaia/db/engine.py`: async engine and session factory. On SQLite, run `PRAGMA journal_mode=WAL`
  and `PRAGMA foreign_keys=ON` on connect.
- `gaia/db/models.py`: SQLAlchemy 2.x declarative models (`Mapped[...]`).
- `gaia/db/repo.py`: the scoped repository helper.
- `gaia/domain/enums.py`, `gaia/domain/schemas.py`: Pydantic read/create schemas.
- `backend/alembic/`: migrations.

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
- [ ] `make migrate` creates the schema in `~/.gaia/gaia.sqlite`.
- [ ] Tests pass. mypy is strict-clean for `gaia/db` and `gaia/domain`.
