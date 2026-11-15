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
- [ ] It works against a live backend with a fake run (backend `GAIA_FAKE_LLM=1` mode, which doc 18 adds; until then, use seeded data).
- [ ] A Playwright smoke test for 17c and 17d.

## Decisions and deviations (as built)

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
