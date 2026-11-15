# Handoff: 18a — Daily budget pause, new-run block, and the kill switch

Filled from `HANDOFF_TEMPLATE.md` for the lead's sandboxed DeepSeek harness (2026-11-14). Your file
tools are confined to the worktree. Your `run` tool is sandboxed: no network, no git writes, no
installs. The lead commits and merges; your report goes through `finish`.

---

You are a junior engineer on **Gaia**, a local-first control plane for AI coding agents. Your task
is the backend of package **18a**: make Gaia safe to leave running. Much of doc 18a already exists:
- per-run budgets (`Run.budget_usd`, default `settings.run_budget_usd`, overridable at `POST /runs`);
- a daily workspace cap (`settings.daily_budget_usd`) enforced by `costs.ensure_budget`, which the
  LLM gateway calls before every provider request;
- per-agent max turns (`junior_max_turns`, `lead_max_turns`);
- the guard's **run**-budget pause with a `budget` approval.

You build the **three missing pieces**:
1. the daily cap pauses a run the same way the run cap does;
2. the daily cap blocks new runs;
3. a kill switch that pauses everything and can resume it.

The UI button is a later package (18a-2). You only build the API.

## 1. Read first, in this order
1. `docs/implementation/18-budgets-observability-hardening.md` (18a) and
   `docs/implementation/12-orchestrator-graph.md` (the guard, interrupts, the 12a lead review's G1
   about budget approvals, and "Live E2E fixes").
2. `docs/implementation/00-README.md`, "Rules for every work package".
3. `docs/backend/orchestrator.md` (gates are interrupts; a node re-runs from its first line on
   resume; crashes and restarts), `api.md`, `database.md`.
4. Code:
   - `gaia/orchestrator/guard.py`: the whole file, especially step 2, the budget check;
   - `gaia/orchestrator/runner.py` (`RunManager`: `start`, `resume`, `cancel`, `recover`, `aclose`,
     `_launch`) and `gaia/orchestrator/approvals.py` (`upsert_approval`, `apply_decision`,
     `expire_pending_approvals`);
   - `gaia/services/costs.py` (`ensure_budget`, `remaining_budget`, `BudgetExceeded`) and
     `gaia/db/repo.py` (`daily_llm_usage_cost_usd`, `RunRepo.list_recoverable`);
   - `gaia/domain/enums.py` (`RunStatus`), `gaia/domain/state.py` (`RUN_TRANSITIONS`,
     `TERMINAL_RUN_STATUSES` in the guard);
   - `gaia/api/routes/runs.py` (`POST /runs`), `routes/approvals.py` (decide), `gaia/api/app.py`
     (the lifespan, where `recover()` is called), `gaia/api/errors.py` (`ErrorCode`);
   - `gaia/config/settings.py` (how paths derive from `data_dir`);
   - tests: `tests/test_orchestrator_guard.py`, `tests/test_orchestrator_runner.py`,
     `tests/test_api_runs.py`, `tests/test_api_decide.py`.

## 2. Lead notes
These are design decisions already made. Follow them, and record any deviation in your report.

### 2.1 The daily cap pauses a run (guard)
Today the guard pauses only when `remaining_budget(run) <= 0`. When the **daily** cap is hit, the
gateway refuses, the step returns `budget_exceeded`, and the next guard lets the run continue, so
it can loop into an escalation. Fix it in the guard's step 2:
- Compute `spent_today` with `daily_llm_usage_cost_usd` over the current UTC day, exactly as
  `ensure_budget` does. **Extract** a helper in `costs.py`, `async def daily_spend(session,
  workspace_id, *, now) -> Decimal`, use it in both places, and inject `now` as `ensure_budget`
  already does. The guard gets its clock from `deps` if one exists; otherwise add an optional
  `now: Callable[[], datetime]` to the guard factory with a `utcnow` default.
- `scope = "run"` if the run cap is spent, else `"daily"` if `spent_today >= daily_budget_usd`,
  else no pause. The run cap is checked first, as in `ensure_budget`.
- The payload gains `"scope"`. For `"daily"` it also gains `"spent_today_usd"` and
  `"daily_budget_usd"` (strings, like the existing money fields).
- **The idempotency key must be unique per pause but stable across the resume re-run.** The node
  re-runs from its first line on resume, and the approval is still `pending` at that point.
  - Run scope keeps today's `f"budget:{node}:{run.cost_usd}"`.
  - Daily scope uses `f"budget:{node}:daily:{n}"`, where `n` is the number of this run's
    **decided** `budget` approvals with daily scope. Count them in `gaia/db/repo.py` and filter on
    `payload_json["scope"]`. If filtering JSON is awkward in SQLAlchemy+SQLite, count decided
    budget approvals whose key starts with `f"budget:{node}:daily:"`; that is the same thing.
  - Explain the key rule in a comment. A test must show that two daily pauses in a row create two
    approval rows, and that the second does not silently reuse the first decided row.
- **Deciding a daily-scope approval:**
  - `reject` cancels the run (as today);
  - `approve` changes **no** budget and simply continues. If the day is still over its cap, the
    next guard pauses again with a new approval. That is correct: the daily cap is a hard limit,
    and approving means "I raised `daily_budget_usd`, or it is a new day; try again". Say so in a
    comment;
  - `budget_usd` on a daily-scope approval is **ignored**, and the reason is logged.
- Run-scope behaviour is unchanged (12a G1).

### 2.2 The daily cap blocks new runs
`POST /api/v1/runs` refuses with **409** and the new `ErrorCode.DAILY_BUDGET_EXHAUSTED`
(`"daily_budget_exhausted"`) when `daily_spend >= daily_budget_usd`. The message states the spend
and the cap. Check in the route, before `RunManager.start`, using `daily_spend`. Queued runs and
the resumption of existing runs are governed by the guard (§2.1), not by this check.

### 2.3 The kill switch
**State.** A new `RunStatus.PAUSED = "paused"` (no migration: the column is a plain string; check
`gaia/db/models.py`). It is a manual, resumable pause.
- `RUN_TRANSITIONS`:
  - every non-terminal status except `paused` may go to `paused`;
  - `paused` may go to `planning`, `implementing`, `reviewing`, `pr_open`, `awaiting_approval`,
    `paused_budget`, `failed` and `cancelled`.
- `paused` is **not** terminal.
- Add `paused` to `RunRepo.list_recoverable`'s ignored set, so **startup never resumes a paused
  run**. Only an explicit resume-all does. A test must prove this.

**A durable global flag.** `gaia/orchestrator/pause.py`, `class PauseSwitch(path: Path)`, with
`path = settings.data_dir / "paused"`:
- `is_paused() -> bool`;
- `pause(reason: str) -> None` writes a small JSON file `{"since": iso, "reason": …}` with mode
  0600, written atomically (temp file + `os.replace`);
- `resume() -> None` deletes it;
- `info() -> PauseInfo | None`.

A file, not a table: no migration, and it survives restarts. Gaia is single-user and local.

**`RunManager`** takes an optional `pause: PauseSwitch | None` (the wiring passes one; existing
tests pass none, meaning never paused).
- `start(...)` and `resume(...)` raise a new `OrchestratorPaused` when `pause.is_paused()`.
- `async def pause_all(*, reason: str) -> list[str]`:
  1. set the flag **first**, so nothing new starts;
  2. for every run with a live task, cancel the task and await it, as `cancel` does;
  3. then, in one session, set each such run, **and every `pending` run without a task**, to
     `paused`, and **expire their pending `command` approvals** (`reason="paused"`). In-turn agent
     approvals die with the cancelled agent. **Leave graph-gate approvals pending:** the run is
     still waiting for them after a resume;
  4. publish `run.status_changed` for each, and `approval.decided {action: "expired"}` per expired
     approval (reuse `_publish_expired`);
  5. return the paused run ids.

  Runs in `awaiting_approval` or `paused_budget` have no task and are **left as they are**, because
  the flag already blocks their decisions (below).
- `async def resume_all() -> list[str]`: clear the flag, then for every `paused` run, the same
  logic as `recover()`:
  - a pending interrupt → `awaiting_approval`;
  - otherwise relaunch from the checkpoint (or the initial state if it has none).

  Factor out the shared per-run logic from `recover()` instead of copying it. Return the ids.
- `recover()` at startup does nothing while the flag is set, and logs `orchestrator.paused` with
  the reason.

**The API**, `gaia/api/routes/admin.py`, token-required like every route:

| Route | Behaviour |
|---|---|
| `GET /api/v1/admin/pause` | `PauseStatus{paused: bool, since: datetime \| None, reason: str \| None}` |
| `POST /api/v1/admin/pause-all` `{reason?: str ≤ 500}` | → 200 `PauseAllResult{paused_run_ids: list[str]}`. Idempotent: pausing again returns the runs it paused this time, usually none. |
| `POST /api/v1/admin/resume-all` | → 200 `ResumeAllResult{resumed_run_ids}`. Resuming when not paused → 200 with an empty list. |

- `runs is None` (orchestrator off) → 503 `orchestrator_unavailable`, as the runs routes do.
- Each mutation writes an audit row (`admin.pause_all`, `admin.resume_all`).
- **While paused:**
  - `POST /runs` → **409** `ErrorCode.ORCHESTRATOR_PAUSED`;
  - `POST /approvals/{id}/decide` for a **graph gate** → 409 `orchestrator_paused`;
  - deciding a **command** approval still works: it is in-turn, and the agent is gone anyway, so
    the broker will report it.

  Map `OrchestratorPaused` in both routes.
- Pause is **global**, not per workspace. There is one local user. Say so in the docstring.
- Add the events `run.paused_all` and `run.resumed_all`? **No.** Reuse `run.status_changed` per
  run, and keep `EventType` unchanged, because a frontend test pins the list.

### 2.4 Tests
- **Guard:** the daily cap pauses with `scope="daily"` and the right payload; run scope still wins
  when both are spent; approving daily scope changes no budget and re-pauses while still over;
  two consecutive daily pauses create two rows. Freeze the clock, and write `llm_usage` rows for
  the spend.
- `POST /runs` at the cap → 409 `daily_budget_exhausted`; below it → 201.
- **`PauseSwitch`:** round trip, 0600 mode, atomic write (a temp file is never left behind).
- **`pause_all`, with the 12a fake graph deps:**
  - a run blocked mid-node, held by an `asyncio.Event` in a fake node, is cancelled and set to
    `paused`;
  - a pending run is set to `paused`;
  - an `awaiting_approval` run is untouched, and its gate approval stays pending;
  - a pending command approval of a paused run is expired, and the event is published.
- `start`, `resume` and gate decides are refused while paused.
- **`resume_all`:** a paused run with a checkpoint interrupt → `awaiting_approval`; a paused run
  without one is relaunched and **completes** with the fake graph.
- **Startup:** `recover()` leaves `paused` runs alone, and does nothing at all while the flag is
  set.
- **API:** the three routes, 503 when the orchestrator is off, audit rows, idempotency.
- Regenerate `frontend/src/api/openapi.json` (`uv run python scripts/export_openapi.py`) and the
  types (`npm run gen:api`, `cwd: "frontend"`).

## 3. Files
**You may create:**
- `gaia/orchestrator/pause.py`, `gaia/api/routes/admin.py`;
- `tests/test_pause_switch.py`, `tests/test_orchestrator_pause.py`, `tests/test_api_admin.py`,
  `tests/test_guard_daily_budget.py`.

**You may edit:**
- `gaia/orchestrator/{guard,runner,wiring}.py`, `gaia/services/costs.py`;
- `gaia/domain/{enums,state,schemas}.py`, `gaia/db/repo.py`;
- `gaia/api/{app,errors}.py` (the router include, `PauseSwitch` construction, error codes);
- `gaia/api/routes/{runs,approvals}.py` (the two refusals only);
- existing tests that assert `RUN_TRANSITIONS` or the status list;
- `frontend/src/api/openapi.json` and `schema.d.ts` (regenerated only);
- `docs/implementation/18-budgets-observability-hardening.md`: add "Decisions and deviations (as
  built) — 18a" directly **under the 18a section**.

**You must not touch:**
- `pyproject.toml`, `uv.lock`, `[tool.*]`;
- `.github/`, migrations;
- `gaia/runtimes/`, `gaia/tools/`, `gaia/llm/`;
- `frontend/src` other than the generated files;
- other docs.

Another junior is building 18b-2 (`GAIA_FAKE_LLM`) in parallel. It edits `settings.py`, the
lifespan in `app.py` and `wiring.py`. Keep your edits there small and in one block each.

## 4. Non-negotiable rules
- **A pause never loses work and never resumes by itself.** Startup must not relaunch a paused run.
- The daily cap is a hard limit: approving never raises it.
- No network, no git and no task cancellation inside a DB session (load → act → write).
- Every mutation writes an audit row; every route needs the token.
- mypy strict; 100% coverage; no new `# type: ignore` or `# noqa` without a reason.

## 5. How to work
1. Read §1, then write a short plan as your first message.
2. Order:
   1. `RunStatus`/transitions and `list_recoverable`;
   2. `daily_spend` and the guard;
   3. `PauseSwitch`;
   4. the `RunManager` methods;
   5. the routes and refusals;
   6. OpenAPI;
   7. the doc section.
3. Checks (from `backend/`): `uv run ruff check .`, `uv run ruff format --check .`, `uv run mypy`,
   `uv run pytest -q` (100% coverage). Frontend (`cwd: "frontend"`), after regenerating:
   `npm run lint`, `npm run typecheck`.

## 6. When blocked
Write the question under "Open questions", choose the most restrictive reasonable behaviour, and
keep going.

## 7. Report (pass to `finish`, in Markdown, with these headings)
Summary · Files changed · Deviations · Security-relevant changes · Tests added · Open questions ·
Final check output.
