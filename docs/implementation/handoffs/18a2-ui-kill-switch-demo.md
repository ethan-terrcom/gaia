# Handoff: 18a-2 — UI for the kill switch, demo mode and daily budget approvals

Filled from `HANDOFF_TEMPLATE.md` for the lead's sandboxed DeepSeek harness (2026-11-15). Your file
tools are confined to the worktree. Your `run` tool is sandboxed: no network, no git writes, no
installs. Pass `argv` as a JSON **array**. The lead commits and merges; your report goes through
`finish`.

---

You are a junior engineer on **Gaia**. The backend now has:
- **a kill switch:** `GET /api/v1/admin/pause` → `{paused, since, reason}`;
  `POST /admin/pause-all` `{reason?}` → `{paused_run_ids}`;
  `POST /admin/resume-all` → `{resumed_run_ids}`. All three return 503 `orchestrator_unavailable`
  when the orchestrator is off. While paused, `POST /runs` and graph-gate decisions answer 409
  `orchestrator_paused`. Runs gain the status **`paused`**.
- **demo mode:** `GET /api/v1/me` returns `demo_mode: bool`.
- **daily-scope budget approvals:** a `budget` approval's `payload_json` now has `scope: "run" |
  "daily"`. Daily scope adds `spent_today_usd` and `daily_budget_usd`. Approving it changes no
  budget; it means "I raised the cap, or it's a new day; try again".

Your task is the UI for these.

## 1. Read first
1. `docs/frontend/README.md` (the shell, shared components, the event stream, the testing
   pattern), `docs/implementation/17-frontend-views.md`, and the 18a and 18b-2 sections of
   `docs/implementation/18-budgets-observability-hardening.md`.
2. Code:
   - `src/layout/AppLayout.tsx` (the top bar: today's spend, theme toggle);
   - `src/api/{client,errors,queryClient}.ts`, `src/api/schema.d.ts` (`PauseStatus`,
     `PauseAllResult`, `ResumeAllResult`, `SessionRead`);
   - `src/hooks/useEventStream.ts` (the `INVALIDATIONS` map);
   - `src/components/{ConfirmDialog,StatusBadge,status,States}.tsx|ts`;
   - `src/views/approvals/ApprovalsView.tsx` (`BudgetKindDetail`, `approve()`), and its tests;
   - how `src/views/*/…test.tsx` stub `fetch`, passing the current global `fetch` per request.

## 2. What to build

### 2.1 The kill switch, in the top bar (`src/layout/PauseControl.tsx`)
- Query `['admin', 'pause']` → `GET /admin/pause`. A **503** means the orchestrator is off: render
  nothing. Any other error: render nothing too. The control is not critical chrome; log nothing
  sensitive.
- **Not paused:** a **Pause all** button (red outline) opens `ConfirmDialog` (`destructive`) with
  an optional reason textarea (≤ 500 characters) and the text: "Stops every running agent now and
  holds every run until you resume. Work in progress restarts from its last completed step."
  Confirming sends `POST /admin/pause-all {reason}`. On success, invalidate `['admin']` and
  `['runs']`; show an inline error on failure.
- **Paused:** a full-width **banner** under the top bar: "All runs are paused since
  <relative time> (<reason>)", with a **Resume all** button that sends `POST /admin/resume-all`
  directly; resuming is safe, so no dialog. On success, invalidate `['admin']` and `['runs']`.
- **Live:** add `run.status_changed` → `['admin']` to `INVALIDATIONS` in `useEventStream.ts`, so
  a pause made in another tab shows up. If `INVALIDATIONS` has a test pinning its contents, update
  it.

### 2.2 The demo badge (`src/layout/DemoBadge.tsx`)
`GET /me` (reuse `fetchMe` or its query key if `App.tsx` already has one; read the code) → when
`demo_mode` is true, an amber **Demo mode** badge in the top bar, with
`title="GAIA_FAKE_LLM=1: no real model is called"`.

### 2.3 The `paused` status
`src/components/status.ts`: `paused` gets the `warn` tone. Test it.

### 2.4 Daily-scope budget approvals (`ApprovalsView.tsx`)
In `BudgetKindDetail`:
- **`scope === "daily"`:** show **Spent today** and **Daily cap**, and the sentence "The daily
  cap is a hard limit. Approve after raising `GAIA_DAILY_BUDGET_USD` or after midnight UTC to
  try again; reject to cancel the run." **Hide** the "new budget" input, and never send
  `budget_usd` for daily scope.
- **`scope === "run"`, or missing (older rows):** unchanged.

### 2.5 409 messages
Where Start run (`src/views/tickets/…`) or a decision (`ApprovalsView`) shows an inline
`ApiError`, the backend's message already explains `orchestrator_paused` and
`daily_budget_exhausted`. Add a test for each that the message is shown; change no code unless
it isn't.

## 3. Files
**You may create:** `src/layout/PauseControl.tsx`, `src/layout/DemoBadge.tsx` and their tests.

**You may edit:**
- `src/layout/AppLayout.tsx` (mount both), `src/hooks/useEventStream.ts` (one invalidation) and
  its test;
- `src/components/status.ts` and its test;
- `src/views/approvals/ApprovalsView.tsx` and its test;
- the tickets view test (§2.5 only);
- `docs/implementation/18-budgets-observability-hardening.md`: add "Decisions and deviations (as
  built) — 18a-2" under the 18a lead review;
- `docs/frontend/README.md` (one line each for the two components).

**You must not touch:**
- the backend;
- `package.json` and lockfiles;
- generated files;
- other views.

## 4. Rules
- Plain-text rendering only, and loading/error states as in the other views. `ConfirmDialog` for
  the destructive action.
- Pass the current global `fetch` per request (the 17e/17f pattern).
- Checks (`cwd: "frontend"`): `npm run lint`, `npm run typecheck`, `npm run test`. All must pass,
  and you must **run them**.

## 5. Report (pass to `finish`)
Summary · Files changed · Deviations · Tests added · Open questions · Final check output (real
output).
