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
4. **Parents are loaded in the caller's workspace (review F5).** Before writing a child row, load its
   parent through `ScopedRepo.get`; an id from another workspace answers 404. Never trust an id from a body.
5. Add an OpenAPI tag per router. Export the schema to `frontend/src/api/openapi.json` with `make openapi`
   (doc 16 generates types from it).

## Tests
API tests with `httpx.AsyncClient` against an in-memory SQLite DB:
- Registering a path that isn't a git repo gives 422.
- A ticket status transition rule is enforced (backlog → done directly gives 409).
- Pagination works. Unknown fields give 422.
- Cross-workspace access gives 404 (not 403, so existence is not leaked).

## Acceptance criteria
- [x] With `curl`, you can register a repo, create a ticket, and list worktrees.

## Decisions and deviations (as built)

1. **Remote detection uses `GitService._invoke`.** The package is not allowed to edit
   `gaia/services/git.py`, so no public `remote_url()` method could be added there. The
   repositories route calls the existing private `_invoke` door for
   `git remote get-url origin`, which still gets the hardened config and runs only after
   `validate_repo` has accepted the path. The lead should promote this to a public
   `GitService` method when a future package owns `git.py` again.
2. **`setup_command` / `test_command` are `list[str] | None`.** The columns are `JSON`
   in migration `0003`, which rebuilds `repository` in batch mode and backfills old text
   values as one-item arrays (`NULL` when empty). The schemas enforce 1–64 items, each
   1–1000 characters, no NUL.
3. **`RepositoryCreate.name` is optional.** The route defaults it to
   `validate_repo(...).name`; the body may override it. Duplicate `name` and duplicate
   resolved `local_path` are both checked in the route and return 409. A database unique
   constraint for `local_path` was deliberately not added because the migration in scope
   only changes the command columns.
4. **Worktree list returns `{items, unknown_paths}`.** Each item adds `orphan`,
   `exists`, `dirty` and `tampered`. A `WorktreeTampered` during the per-row `is_dirty`
   check produces `dirty: null, tampered: true` for that row, not a failed list.
5. **`TicketService` owns parent loading and transitions.** `POST /tickets` loads
   `repo_id` through `ScopedRepo.get` before writing the child, and `PATCH` routes status
   through `transition()`. The route layer only maps `TicketParentNotFound` /
   `TicketNotFound` to 404 and appends the audit row.
6. **The curl acceptance criterion is proven by an httpx end-to-end test** because the
   sandbox has no running server: register a real fixture repo, create a ticket, create a
   worktree through `WorktreeService`, list worktrees with the dirty flag, and remove one.
   The lead can run the equivalent `curl` commands against a live server.
7. **`seed_initial_data` now takes `Settings`.** It seeds `lead`
   (`native_claude`, `settings.lead_model`) and `junior-1` (`native_deepseek`,
   `settings.junior_model`) idempotently by `display_name`. The app lifespan passes
   `settings`; tests pass a `Settings(data_dir=tmp_path)`.
8. **`AppServices` gained `git`**, built once in the lifespan from settings. A
   `WorktreeService` is constructed per request with the request session and the
   authenticated workspace id (`WorktreeDep`).
9. **Error handlers were added for the doc-06 service errors.** `IllegalTransition`,
   `WorktreeDirty` and `WorktreeTampered` map to 409; `NotARepository`, `UnsafePath`
   and `InvalidRef` map to 422. `WorktreeError` maps to 409 so a worktree whose parent
   repository is missing from the caller's workspace cannot become a 500.
10. **Audit rows are written by every mutating route** with `actor="user:<id>"`,
    `action` like `repository.register`, `ticket.update` and `worktree.remove`,
    `target=<row id>` and a small `details_json` built by the route. Request bodies are
    never copied into the audit trail.
11. **The OpenAPI export is generated offline** by `backend/scripts/export_openapi.py`
    and committed at `frontend/src/api/openapi.json`. `make openapi` runs the script and
    is added to `.PHONY`.

**Lead review fixes (2026-11-13, [`../reviews/2026-11-13-review-06.md`](../reviews/2026-11-13-review-06.md)).**
Unregistering a repository that has tickets or live worktrees answers 409, because the row cascades.
Remote detection moved to a public `GitService.remote_url()` that strips URL credentials. Orphan
worktrees skip the dirty and tamper check. The generated `openapi.json` is in `frontend/.prettierignore`.
The `curl` acceptance criterion was verified live by the lead.
