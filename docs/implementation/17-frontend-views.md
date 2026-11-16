# 17 — Frontend Views

## Goal
The Architect's cockpit. **Split this into one PR per view** (17a–17f); each view is a
self-contained task.

## Views
### 17a Dashboard
- Cards for active runs (status, ticket, agent, current step, cost, elapsed time), the pending approvals
  count, and open PRs with CI badges.
- Today's spend broken down by provider.
- Updates live through `useEventStream()`.

### 17b Tickets
- A kanban board by `TicketStatus`, plus a table view. Create and edit with a modal.
- "Start run" button → `POST /runs`, with a choice of junior runtime and a budget.
- Filter by repo and search.

### 17c Run detail (`/runs/:id`): the most important view
- Header: ticket, branch, worktree path (copy button and an "Open in VS Code" `vscode://` link), PR link, cost and budget bar.
- Step list with statuses and attempts.
- Timeline of events: agent messages, tool calls (collapsible), test output. Virtualised list.
- Diff tab using `react-diff-view` or similar, fetched from `/runs/{id}/diff`.
- Cancel button with a confirmation.

### 17d Approvals inbox
- A list of pending approvals, oldest first.
- Per kind:
  - `plan`: render the plan, with inline editing of steps (validated against the schema) → approve / reject / approve with edits.
  - `command`: show the exact argv, cwd and the reason it is being asked for.
  - `pr_open`: the final diff summary and the review.
  - `merge`: requires typing `merge <pr#>`.
  - `budget`: current and requested budget.
- Keyboard: `j`/`k` to move, `a` to approve, `r` to reject (except merge, which always needs typing).


**As built** ([`review-17d`](../reviews/2026-11-14-review-17d.md)): the view lives in
`src/views/approvals/`. Details are shown per kind from `payload_json`:
- an edited plan is validated with zod (1–10 steps) before it is sent;
- each command argument gets its own `<code>`;
- a merge needs `merge <n>` typed in `ConfirmDialog`;
- `a` never merges.

"Decided" uses `GET /approvals?decided=true&order=newest&limit=50`; both parameters were added in
the lead review. Decided items show no decision controls. After a decision, the item is hidden at
once and the selection moves to the next one.

### 17e Pull requests
- A table: PR, ticket, run, CI status, review state, age. A link out to GitHub.

### 17f Worktrees & repos
- Registered repos, and per repo its worktrees with branch, dirty status and orphan flag.
  Remove a worktree with a confirmation, and show a "force" warning when it is dirty.
- "Register repo" form.

## Shared rules
- All agent or repo text is rendered as plain text or sanitized markdown.
- Every destructive action gets a confirmation dialog.
- Empty, loading and error states are required for every view.

## Acceptance criteria (per view)
- [x] It works against a live backend. 17a–17d were checked together in a real browser against
  the API, with the data of live E2E run 1 plus seeded approvals (see the 17d review).
- [ ] A Playwright smoke test for 17c and 17d. It was **run** by the lead (Playwright 1.64.1, all
  checks green). The committed, self-contained version lands with 18b-2's `GAIA_FAKE_LLM=1` demo
  mode, which produces a run's data without seeding.

## Decisions and deviations (as built)

### 17a Dashboard (2026-11-14, [`review-17a`](../reviews/2026-11-14-review-17a.md))
- The view lives in `src/views/dashboard/`. It has five cards, each with its own query, loading,
  error and empty state, so one failing endpoint never blanks the page.
- Query keys use the stream's prefixes (`runs`, `approvals`, `pull-requests`, `costs`). The
  14-day metrics card (`['metrics', …]`) is not live; it refreshes on focus.
- Active runs: `GET /runs?limit=100`, with terminal statuses filtered out client-side.
  **Deviation:** the step shows as "Step n: title", because the list has no step total.
- The cost-per-day bars are plain divs; no chart library.

### 17e Pull requests (2026-11-14, [`review-17e`](../reviews/2026-11-14-review-17e.md))
- The view lives in `src/views/pull-requests/`; `src/pages/PullRequests.tsx` renders it.
- The state filter (Open, the default; Merged; Closed; All) is kept in the URL (`?state=merged`),
  so a filtered view can be linked and survives a reload. "All" omits `state` from the request.
- The query key is `['pull-requests', {state}]`; the global stream's `pr.*`/`ci.*` invalidation
  keeps it live.
- PR numbers link to GitHub only when the URL starts with `https://github.com/`, with
  `target="_blank" rel="noreferrer noopener"`; anything else renders as text.
- **Deviation:** the query passes the current global `fetch` per request. openapi-fetch captures
  `globalThis.fetch` when the client is created, and passing it per call lets tests stub `fetch`
  after import. In production it is the same function, and the middleware still applies.

### 17b Tickets (2026-11-14, [`review-17b`](../reviews/2026-11-14-review-17b.md))
- The view lives in `src/views/tickets/`. The board (default) and table are toggled with
  `?view=table`; the repository filter and search are `?repo=` and `?q=`. Search uses the API's
  `q` parameter, and typing replaces the history entry (lead fix).
- Create and edit happen in a modal (`POST`/`PATCH /tickets`). "Start run" sends only the fields
  that were set (`junior_runtime`, `budget_usd` > 0) and navigates to `/runs/<id>`. A 409 (active
  run) and a 503 (orchestrator off) show inline.
- Pagination is cursor-based, with "Load more".

### 17f Worktrees & repositories (2026-11-14, [`review-17f`](../reviews/2026-11-14-review-17f.md))
- The view lives in `src/views/worktrees/`, with two sections: repositories (list, register,
  unregister) and worktrees (table, filters, remove).
- **"All repositories" fans out**, with one `GET /api/v1/worktrees?repo_id=` per repository, merged
  client-side: the endpoint requires `repo_id`. A server-side "all" would make the same per-repo
  git calls, so this is not a performance regression. The query key stays `['worktrees', …]`, so
  live `worktree.*` events still refresh it.
- Commands are typed as one line and split on whitespace into argv (no shell); empty means none.
- Removing a **dirty** worktree warns, sends `force=true`, and requires typing the branch name. A
  clean one sends `force=false`.
- The view passes the current global `fetch` per request, as 17e does (testability; identical in
  production).
- **Lead fix:** the root `.gitignore` had `worktrees/`, which also ignored
  `frontend/src/views/worktrees/`. The view's files were invisible to git. It is now
  `/worktrees/`, anchored to the repository root as intended.

### 17c Run detail (2026-11-14, [`review-17c`](../reviews/2026-11-14-review-17c.md))
- The view lives in `src/views/run-detail/`, and the timeline summaries in `events.ts`.
- **The tab is in the URL** (`?tab=diff`), so the approvals inbox links straight to a run's diff
  (lead fix).
- Timeline: the history comes from `GET /runs/{id}/events` (pages of 500) and is merged with live
  events by `seq`. It renders through a dependency-free windowed list with fixed 36 px rows. Tool
  calls and results expand to the pretty-printed payload, as text.
- The query keys are `['runs', id]` (live), plus `['run-events', id]` and `['run-diff', id]`. The
  last two are deliberately outside the `runs` prefix, so a live event doesn't refetch the whole
  history or diff.
- The diff is shown as text with per-line classes; no diff library. The truncation marker shows a
  note.
- Cancel sits behind `ConfirmDialog` and is hidden for terminal runs.

### Runs list (`/runs`) (2026-11-15, bug fix)
- **Gap in the plan:** no package owned the `/runs` list. 17c built the run detail only, so the
  nav's Runs link (and the `g r` hotkey) still showed the 16 placeholder ("Arrives in 17c"). The
  owner hit it on returning to Runs from a run.
- The view lives in `src/views/runs/`. It is a table (ticket → `/runs/:id`, status, current step,
  cost of budget, PR number, started), newest first, over `GET /runs` with cursor pagination
  ("Load more", 50 a page).
- Filters are All, Active, Failed, Completed and Cancelled, kept in `?status=`. The terminal
  statuses go to the API; **Active** is filtered client-side over all runs, because the API takes
  one status and "active" is every non-terminal one. Unknown values fall back to All.
- "Started" shows `started_at`, falling back to `created_at` for a run that failed before the
  graph started.
- The query key starts with `runs`, so the stream's `run.*`/`step.*` invalidation keeps it live.
- Settings also still said "Arrives in 17f" (17f shipped Worktrees); it now has a plain header,
  and the unused `PagePlaceholder` is gone.
