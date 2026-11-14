# 06 — Repositories & Tickets API

## Goal
CRUD endpoints for repositories, tickets, agents and worktrees. These are the read
models the dashboard needs.

## Out of scope
Starting runs (12/14) and importing GitHub Issues (15).

## Files
- `gaia/api/routes/{repositories,tickets,agents,worktrees}.py`
- `gaia/services/tickets.py`

## Endpoints (all under `/api/v1`, all auth'd, all workspace-scoped)
| Method | Path | Notes |
|---|---|---|
| POST | `/repositories` | body `{local_path, setup_command?, test_command?}` (argv lists, owner-registered, never read from the repo). Validate with `GitService.validate_repo`; detect the remote and default branch |
| GET | `/repositories` | |
| GET | `/repositories/{id}` | includes worktree count and active runs |
| DELETE | `/repositories/{id}` | unregister only; **never** delete files on disk |
| POST/GET/PATCH | `/tickets`, `/tickets/{id}` | PATCH status goes through `transition()` |
| GET | `/tickets?status=&repo_id=&q=` | paginated (`limit` ≤ 100, cursor) |
| GET | `/agents` | the agent roster with live status (static rows for now) |
| GET | `/worktrees?repo_id=` | from `WorktreeService.list`, including orphan flags; add `dirty` here with one `GitService.is_dirty` per row (doc 05 deviation 7 - it is deliberately not in `list()`) |
| DELETE | `/worktrees/{id}?force=` | writes an audit log entry |

## Tasks
1. Pydantic request models with `extra="forbid"` and length limits (title ≤ 200, body ≤ 50k).
2. Every mutating endpoint writes an `audit_log` row.
3. Seed two agents at first start: `lead` (claude) and `junior-1` (deepseek).
4. Add an OpenAPI tag per router. Export the schema to `frontend/src/api/openapi.json` with `make openapi`
   (doc 16 generates types from it).

## Tests
API tests with `httpx.AsyncClient` against an in-memory SQLite DB:
- Registering a path that isn't a git repo gives 422.
- A ticket status transition rule is enforced (backlog → done directly gives 409).
- Pagination works. Unknown fields give 422.
- Cross-workspace access gives 404 (not 403, so existence is not leaked).

## Acceptance criteria
- [ ] With `curl`, you can register a repo, create a ticket, and list worktrees.
