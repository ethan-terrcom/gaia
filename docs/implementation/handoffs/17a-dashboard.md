# Handoff: 17a — Dashboard

Filled from `HANDOFF_TEMPLATE.md` for the lead's sandboxed DeepSeek harness (2026-11-14). Your file
tools are confined to the worktree. Your `run` tool is sandboxed: no network, no installs. You can
run `npm run …` with `cwd: "frontend"`. The lead commits; your report goes through `finish`.

You are a junior engineer on **Gaia**. Build exactly one frontend view, **17a: Dashboard**, at
`/`. It replaces the placeholder in `src/pages/Dashboard.tsx`. Read `docs/frontend/README.md` and
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


## The view
The first screen: what is happening right now, and what needs me.
- **Active runs:** `GET /api/v1/runs?status=…`. Show runs that are not terminal: fetch without a
  status filter and drop `completed`, `failed` and `cancelled` client-side, or use the filter if
  14's schema supports a list of statuses (read `schema.d.ts`). Each card shows the ticket title,
  a `StatusBadge` of the run status, the current step (index/total and title), `formatUsd(cost_usd)`
  of `formatUsd(budget_usd)` with a small bar, and the elapsed time since `started_at`
  (`formatRelative`). It links to `/runs/<id>`. Query key `['runs', {view: 'active'}]`.
- **Needs you:** the pending approvals count (reuse `fetchPendingApprovals`, query key
  `['approvals','pending']`) as a large number, linking to `/approvals`, with the oldest pending
  item's kind and age if present (`GET /api/v1/approvals?status=pending&limit=1`).
- **Open PRs:** `GET /api/v1/pull-requests?state=open&limit=5`, as rows with a CI badge, linking
  to `/prs`.
- **Today's spend by provider:** `fetchTodayCosts()`, as `by_provider` rows with amounts, and the
  total against the budget.
- **Last 14 days:** `GET /api/v1/metrics/summary?days=14`. Show the success rate (or "—"), the
  average cost per run, and a **simple bar list** of `cost_by_day`: plain divs with widths, **no
  chart library**.
- Everything is live through the global stream's invalidations (runs, approvals, pull-requests,
  costs).
- **Empty state** with no runs ever: "No runs yet. Create a ticket and press Start run."

## Tests
Each card renders from stubbed responses; each card has its own loading and error state, and one
failing card does not blank the page; the empty state; the links.
