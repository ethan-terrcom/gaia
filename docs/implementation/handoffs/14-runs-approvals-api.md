# Handoff: 14 — Runs & approvals API

Filled from `HANDOFF_TEMPLATE.md` for the lead's sandboxed DeepSeek harness (2026-11-14). Your file
tools are confined to the worktree. Your `run` tool is sandboxed: no network, no git writes, no
installs. The lead commits and merges; your report goes through `finish`.

---

You are a junior engineer on **Gaia**, a local-first control plane for AI coding agents. Your task
is package **14**: the HTTP API that starts, watches and cancels runs, and lets a human **decide
approvals**. After it, a full run can be driven with `curl`, which is doc 14's acceptance
criterion.

The composition root that puts a real `RunManager` in the app is **12b-3**, built in parallel.
You build against the interface in §2.1 and test with the 12a **fake** graph deps. The read-only
approvals list (`GET /api/v1/approvals`) already exists (16); you add the decision endpoint
beside it.

## 1. Read first, in this order
1. `docs/implementation/14-approvals-api.md`: your contract.
2. `docs/implementation/12-orchestrator-graph.md`: the 12a decisions (the `Decision` shape, the
   idempotency keys, the node → status table, `RunBusy`).
3. `docs/implementation/10-agent-runtimes.md`: the 10a lead review (`DbApprovalBroker.decide`,
   `ApprovalNotPending`, the D1–D3 rules).
4. `docs/implementation/00-README.md`, "Rules for every work package".
5. `docs/backend/api.md`, `orchestrator.md`, `runtimes.md`, `database.md`.
6. `docs/reviews/2026-11-14-review-12a.md` (the 12b/14 open items it lists) and
   `…-review-10a.md`.
7. Code:
   - `gaia/orchestrator/{runner,approvals,deps,graph}.py` and `nodes/approve_*.py`;
   - `gaia/runtimes/approvals.py`;
   - `gaia/api/{app,deps,errors}.py`, `gaia/api/routes/{approvals,tickets}.py`;
   - `gaia/domain/{schemas,enums,plans,state}.py`, `gaia/db/{models,repo}.py`;
   - `gaia/services/{git,worktrees}.py`;
   - tests: `tests/test_orchestrator_*.py` (the 12a fakes: reuse them), `tests/test_api_approvals.py`,
     `tests/conftest.py` (how the app is built in tests).

## 2. Lead notes for this package
These are design decisions already made. Follow them, and record any deviation in your report.

### 2.1 What you build against (12b-3 provides it in production)
- `AppServices.runs: RunManager | None`, with `start(ticket_id, *, workspace_id, created_by) ->
  run_id`, `resume(run_id, decision: dict)` (raises `RunBusy`) and `cancel(run_id, *,
  workspace_id)`.
- `AppServices.approvals: DbApprovalBroker | None`, with `decide(workspace_id, approval_id,
  approved, comment, user_id)` (raises `ApprovalNotPending` or `LookupError`).
- If 12b-3 has not landed on your branch, **add these two optional fields to `AppServices`
  yourself** (`None` defaults, same names) and note it; the lead merges the two.
- `runs is None` (the orchestrator is disabled or not built) → the run and decision endpoints
  return **503** `{"error": {"code": "orchestrator_unavailable", …}}`. Add the error code to the
  envelope's code enum in `gaia/api/errors.py`, following its existing pattern.
- **Tests:** build a real `RunManager` over the **12a fake** `GraphDeps` plus a temp
  `AsyncSqliteSaver`, and put it (and an `InMemory`/`Db` broker) on `app.state.services` in a
  fixture. Read how existing API tests reach `app.state.services`.

### 2.2 `gaia/api/routes/runs.py`
| Method & path | Behaviour |
|---|---|
| `POST /api/v1/runs` `{ticket_id, junior_runtime?, budget_usd?}` | `RunManager.start(...)`. Extend `start` with optional `junior_runtime: RuntimeKind \| None` and `budget_usd: Decimal \| None`: the run row gets the given runtime, and the budget (> 0) or `settings.run_budget_usd`. 404 for an unknown or other-workspace ticket; **409** if the ticket already has an active run (pending, planning, implementing, reviewing, awaiting_approval, paused_budget or pr_open; check `RUN_TRANSITIONS`/the terminal set). → **201** `RunRead`. |
| `GET /api/v1/runs?status=&limit=&cursor=` | Newest first, workspace-scoped, paginated like tickets (`TicketPage` pattern). Each item is `RunSummary` = `RunRead` plus `ticket_title`, `current_step` (index and title of the first non-done step, or null), `pr_number`, `pr_url` and `ci_status`, in **one joined query** in `gaia/db/repo.py`. |
| `GET /api/v1/runs/{id}` | `RunDetail`: the run, ticket summary, `steps: RunStepRead[]` (ordered), worktree (`branch`, `path`, `status`), `approvals: ApprovalRead[]` (oldest first) and `pull_request` (or null). 404 if absent or in another workspace. |
| `POST /api/v1/runs/{id}/cancel` | `RunManager.cancel(...)`. The worktree is kept (doc 14). Cancelling a terminal run → 409 (`RunStatus` transition error; reuse `domain/state`'s exception). → 200 `RunRead`. |
| `GET /api/v1/runs/{id}/diff?max_bytes=` | Text: the committed diff `GitService.diff(location, worktree.base_sha, "HEAD")`, then a blank line, then the **uncommitted** diff `GitService.diff_worktree(location)`. **Never** `diff_since`: it stages intent-to-add entries in the index, and a GET must not change state. Cap at `max_bytes` (1 KB–2 MB, default 500 KB) with a trailing `\n[diff truncated]`. `Content-Type: text/x-diff; charset=utf-8`. 404 if the run has no worktree. Git runs outside any DB session. |

Every mutating route writes an **audit row** (`AuditLogRepo`, actor `user:<id>`, action `run.start`
or `run.cancel`, target the run id), as the existing routes do (look at `tickets.py`).

### 2.3 `POST /api/v1/approvals/{id}/decide` (in `routes/approvals.py`)
Body `DecideRequest{decision: "approve"|"reject"|"edit", comment?: str (≤ 4000), edited_payload?:
dict, confirm?: str, budget_usd?: Decimal}`.
1. Load the approval (scoped). Missing or other workspace → **404**. Status not `pending` →
   **409** `approval_not_pending`.
2. **Per kind:**
   - **`command`** (in-turn): `decision` must be `approve` or `reject` (`edit` → 422). Call
     `services.approvals.decide(workspace_id, id, approved=…, comment=comment or "",
     user_id=principal.user_id)`. Map `ApprovalNotPending` → 409 and `LookupError` → 404. The graph
     is **not** resumed (doc 14).
   - **Graph gates** (`plan`, `escalation`, `budget`, `pr_open`, `merge`): build the 12a
     `Decision` (`gaia.orchestrator.approvals.Decision`): `action`, `comment`,
     `decided_by=principal.user_id`, plus:
     - `plan` + `edit`: validate `edited_payload` with `Plan.model_validate` → **422** with the
       first error on failure; pass it as `plan`. `edit` on any other kind → 422;
     - `budget` + `approve`: pass `budget_usd` if given (> 0; the graph refuses a cap below the
       spend, 12a G1);
     - **`merge`**: require `confirm == f"merge {pr_number}"`, where `pr_number` comes from the
       approval's `payload_json` (read the `approve_merge` node for the key name). Anything else
       → **422** `merge_confirmation_required` (doc 14 task 2). Required for `approve` only.

     Then `await services.runs.resume(approval.run_id, decision.model_dump(mode="json"))`, and
     map `RunBusy` → **409** `run_busy`. The node applies the decision to the row on resume (12a);
     **do not** mark the row decided yourself.
3. **Double-decide protection:** keep a module-level `asyncio.Lock` per approval id
   (`dict[str, asyncio.Lock]`, cleaned up after use) around steps 1–2, so two concurrent requests
   cannot both see `pending`. SQLite has no `SELECT … FOR UPDATE`; record this as a deviation. The
   second request then sees `RunBusy` or a decided row → 409.
4. Audit row: `approval.decide`, details `{kind, decision}`. Do **not** put `edited_payload` in
   the audit details; store its sha256 instead.
5. → **202** `{approval_id, status: "accepted"}` for graph gates, because the graph applies it
   asynchronously, and **200** with the updated `ApprovalRead` for `command`.

### 2.4 Showing exactly what is being approved (doc 14 task 3)
`ApprovalRead.payload_json` already carries the payload. Make sure `command` approvals show `argv`,
`cwd` and `reason` (09b puts them there) and `budget` approvals show the figures. Add a test per
kind that asserts the fields are present in `GET /approvals`. No new schema is needed.

### 2.5 Tests
- **The end-to-end acceptance, through HTTP only:**
  1. `POST /runs`, then poll `GET /runs/{id}` until `awaiting_approval`;
  2. `GET /approvals?status=pending` → the plan approval → `POST /decide approve`;
  3. then the PR and merge approvals, the merge with `confirm`;
  4. poll until `completed`.

  This uses the fake deps.
- Deciding twice → 409. Deciding in another workspace → 404. An invalid edited plan → 422. A merge
  without or with a wrong `confirm` → 422. `edit` on a command → 422. `RunBusy` → 409.
- A command approval → `decide` called on the broker with the principal's user id; the graph is
  not resumed.
- `POST /runs`: an unknown ticket → 404; an active run → 409; a custom budget → stored.
  `GET /runs`: the filter, pagination, joined fields, one query (a `count` of SQL statements if
  the suite has a helper; otherwise state it in a comment). Cancel, and cancel of a terminal run →
  409.
- `/diff`: committed plus uncommitted diff, the cap, no worktree → 404, and the GET leaves `git
  status` unchanged (assert the index before and after).
- `runs is None` → 503 on all run and decide routes.
- Regenerate `frontend/src/api/openapi.json` (`uv run python scripts/export_openapi.py`) and the
  types (`npm run gen:api`, `cwd: "frontend"`).

## 3. Files
**You may create:**
- `gaia/api/routes/runs.py`;
- tests: `tests/test_api_runs.py`, `tests/test_api_decide.py`, `tests/test_api_run_e2e.py`.

**You may edit:**
- `gaia/api/routes/approvals.py` (the decide route), `gaia/api/app.py` (router includes),
  `gaia/api/deps.py` (only if 12b-3's fields are absent), `gaia/api/errors.py` (the new codes);
- `gaia/orchestrator/runner.py` (`start`'s two optional arguments only);
- `gaia/db/repo.py` (run queries), `gaia/domain/schemas.py` (`RunSummary`, `RunPage`,
  `RunDetail`, `DecideRequest`, `DecideAccepted`, `RunCreate`);
- `frontend/src/api/openapi.json` and `schema.d.ts` (regenerated only);
- `docs/implementation/14-approvals-api.md`: add "Decisions and deviations (as built)".

**You must not touch:**
- `pyproject.toml`, `uv.lock`, `[tool.*]`;
- `.github/`, migrations;
- `gaia/orchestrator/` except `runner.py`'s `start`;
- `gaia/runtimes/`, `gaia/tools/`, `gaia/llm/`;
- `frontend/src` except the generated files;
- other docs.

## 4. Non-negotiable rules
- Every route requires the token and is workspace-scoped; every mutation writes an audit row.
- **A merge needs the typed confirmation.** No exceptions, and no default.
- GET routes never change state (the `/diff` index rule).
- No git call inside a DB session (load → work → respond).
- mypy strict; 100% coverage; no new `# type: ignore` or `# noqa` without a reason.

## 5. How to work
1. Read §1, then write a short plan as your first message.
2. Order: schemas → `RunManager.start` arguments → the runs routes → decide → the e2e test →
   openapi.
3. Checks (from `backend/`): `uv run ruff check .`, `uv run ruff format --check .`,
   `uv run mypy`, `uv run pytest -q` (100% coverage). Frontend (`cwd: "frontend"`):
   `npm run lint`, `npm run typecheck`.

## 6. When blocked
Write the question under "Open questions", choose the most restrictive reasonable behaviour, and
keep going.

## 7. Report (pass to `finish`, in Markdown, with these headings)
Summary · Files changed · Deviations · External APIs used · Security-relevant changes · Tests added ·
Open questions · Final check output.
