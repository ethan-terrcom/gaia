# Database

> Package 03 · Source of truth: [`docs/implementation/03-database-models.md`](../implementation/03-database-models.md)
> · Concepts used: [sessions, flush and commit](python-concepts.md#sqlalchemy-sessions-flush-and-commit),
> [Alembic](python-concepts.md#alembic-migrations), [generics](python-concepts.md#generics-with-type-parameters),
> [StrEnum](python-concepts.md#strenum), [UUIDv7](python-concepts.md#uuidv7-identifiers),
> [context managers](python-concepts.md#context-managers-and-async-with)

## What it's for

The database records everything Gaia knows: workspaces, users, repositories, tickets, agents,
worktrees, runs and their steps, approvals, pull requests, LLM usage, events and the audit trail.
This layer owns three guarantees: **every query is scoped to a workspace**, **statuses only move
along legal paths**, and **the schema is portable** from SQLite today to Postgres later.

## Files

| File | Contents |
|---|---|
| `gaia/db/engine.py` | `create_engine()`, `create_session_factory()`, `session_scope()`, SQLite pragmas |
| `gaia/db/models.py` | The 13 SQLAlchemy models and their mixins |
| `gaia/db/repo.py` | `ScopedRepo`, `AppendOnlyRepo`, `EventRepo`, `AuditLogRepo` |
| `gaia/db/seed.py` | `seed_initial_data()`: the default workspace and local owner |
| `gaia/db/ids.py`, `gaia/db/types.py` | `uuid7()`, `UtcDateTime`, `utcnow()` |
| `gaia/domain/enums.py` | Every status and kind, as `StrEnum`s |
| `gaia/domain/state.py` | The transition tables and `transition()` |
| `gaia/domain/schemas.py` | Pydantic read and create schemas |
| `alembic/` | Migrations; `make migrate` applies them |

## How it works

### The tables at a glance

```
workspace ─┬─ users
           ├─ repository ─┬─ ticket ── run ─┬─ run_step
           │              └─ worktree ──────┤  approval
           ├─ agent                         ├─ pull_request
           ├─ audit_log  (append-only)      ├─ llm_usage
           └─ event      (append-only, ordered by seq)
```

Every table except `workspace` carries `workspace_id`. Ids are UUIDv7 strings, timestamps are
timezone-aware UTC, and money is `Numeric(12, 6)`, which reads back as `Decimal` and never `float`.

### Reading and writing: `ScopedRepo`

You never write `select(Ticket)` in a service or a route. You ask for a repository bound to a
workspace:

```python
tickets = ScopedRepo(session, Ticket, workspace_id)
ticket = await tickets.get(ticket_id)          # None if missing OR in another workspace
open_ = await tickets.list(status="ready", limit=50)
new = await tickets.create(repo_id=repo.id, title="Add a flag")
await tickets.update(new.id, priority=2)
```

- Every statement includes `WHERE workspace_id = <the repo's>`. A row in another workspace looks
  exactly like a row that doesn't exist, so its existence isn't leaked.
- `create`/`update` refuse a `workspace_id` that isn't the repository's own (`WorkspaceMismatch`).
- Writes `flush()` but never `commit()`. The caller owns the transaction: `session_scope()`, or the
  API's per-request session.
- `event` and `audit_log` get an `AppendOnlyRepo`, which only has `append` and `list`, because a
  trail that can be rewritten isn't a trail.

In the API, routes receive a `Scoped` object (`api/deps.py`) whose `.repo(Model)` builds these
already bound to the caller's workspace. See [api.md](api.md).

### Moving a status: `transition()`

```python
from gaia.domain.state import transition
transition(run, RunStatus.IMPLEMENTING)   # raises IllegalTransition if not allowed
```

The legal moves for `ticket`, `run` and `run_step` are dicts in `domain/state.py`. Never assign
`row.status = ...` directly. Moving to the status a row already has is a no-op, because workflow
nodes may re-run after a crash. Terminal statuses (`done`, `completed`, `failed`, `cancelled`)
accept nothing; review finding F3 proposes a controlled retry path out of `failed`.

### Startup and migrations

`make migrate` runs Alembic, which reads the database URL from `Settings` (never from
`alembic.ini`), so the CLI, the app and migrations always agree on which file to use. On startup the
API seeds one workspace (`slug="local"`) and one owner. Running the seed again is a no-op. An
unmigrated database fails at startup with "run `make migrate`".

## Why it's built this way

- **Statuses are stored as text, not database enums.** Adding a value to a Postgres enum needs a
  migration, and SQLite has no enums at all. Validation happens at the two write boundaries instead:
  the Pydantic schemas and `transition()`.
- **`event.seq` is the primary key.** SQLite only auto-increments an `INTEGER PRIMARY KEY`, and the
  live event stream needs a strictly increasing cursor for "resume from event N". The UUID `id` is
  kept, and unique, for de-duplication.
- **`users`, not `user`; `step_index`, not `index`.** `user` and `index` are reserved words in
  Postgres.
- **SQLite pragmas on every connection.** `foreign_keys=ON`, because SQLite otherwise ignores foreign
  keys. `journal_mode=WAL`, so the dashboard can read while a run writes.
- **`expire_on_commit=False`.** Services return rows after committing, and in async code reading an
  expired attribute would be an error.

## Changing it safely

- **New column or table:** edit `models.py`, then `cd backend && uv run alembic revision --autogenerate -m "..."`.
  **Read the generated script**: check the types are portable, the constraint names follow the
  naming convention, and that it doesn't drop something by accident. Never edit a merged migration.
  `tests/test_migrations.py` fails if the models and migrations drift apart.
- **New status:** add it to the enum *and* to the transition table, with tests in
  `tests/test_domain_state.py`.
- **New tenant table:** use the `UUIDPrimaryKey, WorkspaceScoped, Timestamps` mixins, and reach it
  only through `ScopedRepo`.
- **Known gaps** (review 2026-11-13): approvals lack an idempotency key (F4), and nothing yet stops
  a child row from pointing at a parent in another workspace (F5). Until F5 is fixed, **load the parent
  through `ScopedRepo.get` before you create a child**.
