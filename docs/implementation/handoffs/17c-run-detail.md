# Handoff: 17c — Run detail

Filled from `HANDOFF_TEMPLATE.md` for the lead's sandboxed DeepSeek harness (2026-11-14). Your file
tools are confined to the worktree. Your `run` tool is sandboxed: no network, no installs. You can
run `npm run …` with `cwd: "frontend"`. The lead commits; your report goes through `finish`.

You are a junior engineer on **Gaia**. Build exactly one frontend view, **17c: Run detail**, at
`/runs/:id`. It replaces the placeholder in `src/pages/RunDetail.tsx`. Read `docs/frontend/README.md` and
the two finished views (`src/views/pull-requests/`, `src/views/worktrees/`) first: copy their
patterns, including passing the current global `fetch` per request (recorded in doc 17).

## Common rules for every 17x view (read before the view-specific notes)
- **Read first:**
  - `docs/implementation/17-frontend-views.md` ("Shared rules");
  - `docs/frontend/README.md` (the whole guide, especially "Shared building blocks");
  - `frontend/src/{App,routes}.tsx`, `src/layout/AppLayout.tsx`, `src/api/{client,errors,queryClient}.ts`,
    `src/hooks/useEventStream.ts` (the `INVALIDATIONS` map), `src/components/*`;
  - the generated `src/api/schema.d.ts` for the exact request and response types;
  - `docs/reviews/2026-11-14-review-16.md`.
- **Data:** React Query (`useQuery`, `useMutation`, `invalidateQueries`) over the typed `api` client
  (`api.GET('/api/v1/…', { params: { query: … } })`). Unwrap `{data}` and throw on error, the way
  `fetchPendingApprovals` does, so React Query sees failures. **No hand-written fetch**, no
  absolute URLs.
- **Query keys** must match `INVALIDATIONS`, so live events refresh the view: `['pull-requests']`,
  `['worktrees']`, `['runs']`, `['approvals']`, `['costs']`. Use e.g. `['pull-requests', {state}]`
  for a filtered variant; the prefix still matches.
- **States:** every view handles loading (`LoadingState`), error (`ErrorState` with retry) and empty
  (`EmptyState`, with a sentence on what to do next). Required by doc 17.
- **Untrusted text** (ticket titles, branch names, paths, anything from a repo or an agent) is
  rendered as **plain text**: no `dangerouslySetInnerHTML` and no Markdown in these views.
- **Destructive actions** always go through `ConfirmDialog`.
- **Links out** to GitHub use `target="_blank" rel="noreferrer noopener"`, and only for URLs that
  start with `https://github.com/` (check before rendering an `<a>`).
- **Accessibility:** tables use `<table>`, `<th scope="col">`; buttons are `<button type="button">`;
  every form control has a label.
- **Tests (Vitest + Testing Library)** stub `fetch` (see `src/api/client.test.ts` for the pattern) and
  wrap renders in a `QueryClientProvider` with `makeQueryClient()` and a `MemoryRouter`. Cover the
  three states, the main interaction, and the rendered data.
- **You may touch:** your view's page file in `src/pages/`, new files under `src/views/<view>/`, and
  `src/routes.tsx` (only to point the route at your page). **You must not touch:** `src/components/`,
  `src/api/`, `src/hooks/`, `src/layout/`, `package.json`, the lockfiles, `backend/`, or other views'
  files. Another junior builds another view in parallel; if you need a new shared component, write
  it inside your view folder and mention it in the report.
- **Checks** (`cwd: "frontend"`): `npm run format`, then `npm run lint` (warnings are errors),
  `npm run typecheck`, `npm run test`, `npm run build`. All must pass.
- **Report** (pass to `finish`): Summary · Files changed · Deviations · Tests added · Open
  questions · Final check output.


## The view: the most important screen in Gaia
- **Data:** `GET /api/v1/runs/{id}` (`RunDetail`: run, ticket, steps, worktree, approvals,
  pull_request), query key `['runs', id]`. It is invalidated by the stream's `run.*` and `step.*`
  events for this run.
- **Header:**
  - the ticket title, a `StatusBadge` of the run status, and the branch (mono);
  - the worktree path with a copy button and a `vscode://file/…` link (reuse 17f's approach; copy
    the small helper into your folder);
  - the PR link (GitHub-guarded, like 17e) with a CI badge;
  - **cost against budget** (`formatUsd`) with a bar;
  - **Cancel run**: a `ConfirmDialog` → `POST /api/v1/runs/{id}/cancel`, shown only while the run
    is not terminal. On error, show the message.
- **Steps:** an ordered list with index, title, a status badge, attempts, and `result_summary` as
  plain text.
- **Timeline** (a tab): this run's live events. Use `useEventStream({ runId: id })`, its
  `events`, plus history from `GET /api/v1/runs/{id}/events?after=&limit=` (13's paginated
  replay) on mount. Merge them by `seq` without duplicates.
  - Render each event as a row: time, type and a one-line summary (`agent.message` → `content`;
    `agent.tool_call` → the tool name and summarised args; `agent.tool_result` → ok/decision;
    `run.status_changed` → the new status).
  - Tool calls and results are **collapsible** (`<details>`), showing the payload as
    pretty-printed JSON in a `<pre>`, **as text**.
  - **Virtualise** with a simple windowed list: compute the visible slice from `scrollTop`, with
    fixed row height for collapsed rows. Do **not** add a dependency. A run can have thousands of
    events.
- **Diff** (a tab): `GET /api/v1/runs/{id}/diff` as text. Render it in a `<pre>` with per-line
  classes: `+` green, `-` red, `@@` muted, `diff --git` bold. No diff library; the response is
  plain text. Show the truncation note if present.
- **Approvals** (a section): this run's approvals, oldest first, with kind, status, and a link to
  `/approvals` for pending ones.

## Tests
- The header renders, with cancel confirm → POST, and is hidden when terminal.
- Steps render.
- Timeline: history and live events merge without duplicates by `seq`; collapsible tool rows;
  windowing renders only a slice for 2,000 events; event payloads render as text (a `<script>`
  string stays text).
- Diff: line classes and the truncation note.
- The loading, error and 404 states (an `ApiError` 404 → "Run not found").

The lead adds a Playwright smoke test for this view afterwards; give the main regions stable
`data-testid`s (`run-header`, `run-steps`, `run-timeline`, `run-diff`).
