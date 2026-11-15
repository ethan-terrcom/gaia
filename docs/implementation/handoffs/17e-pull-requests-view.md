# Handoff: 17e — Pull requests view

Filled from `HANDOFF_TEMPLATE.md` for the lead's sandboxed DeepSeek harness (2026-11-14). Your file
tools are confined to the worktree. Your `run` tool is sandboxed: no network, no installs. You can
run `npm run …` with `cwd: "frontend"`. The lead commits; your report goes through `finish`.

You are a junior engineer on **Gaia**. Build exactly one frontend view, **17e: Pull requests**, at
`/prs`. It replaces the placeholder in `src/pages/PullRequests.tsx`.

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
- **Data:** `GET /api/v1/pull-requests?state=&limit=` (`PullRequestPage{items: PullRequestRead[]}`).
  Each item has `number, url, state, ci_status, review_state, head_sha, run_id, run_status,
  ticket_id, ticket_title, repo_name, created_at, updated_at`. The query key is
  `['pull-requests', {state}]`.
- **Filter:** a segmented control with Open (default), Merged, Closed and All. "All" omits `state`.
  Keep the choice in the URL query string (`?state=merged`) with `useSearchParams` from
  `react-router`, so the view can be linked and survives a reload.
- **Table columns:**
  - **PR:** `#number`, linking to `url` in a new tab, only if it starts with `https://github.com/`;
  - **Ticket:** `ticket_title`, as plain text;
  - **Repo;**
  - **Run:** a link to `/runs/<run_id>` with a `StatusBadge` of `run_status`;
  - **CI:** a `StatusBadge` of `ci_status`;
  - **Review:** a `StatusBadge` of `review_state` (`approved`, `changes_requested`, or `none`/null
    shown as "—");
  - **Age:** `formatRelative(created_at)`, with the full date in a `title` tooltip.
- **Live:** the global event stream already invalidates `['pull-requests']` on `pr.*` and `ci.*`
  events, so nothing more is needed. Say in a code comment that it is live through
  `INVALIDATIONS`.
- **Empty state:** "No pull requests yet. Gaia opens a draft PR when a run's final review is
  approved."

## Tests (`src/views/pull-requests/PullRequests.test.tsx`)
- rows render with all columns;
- the GitHub link has `target=_blank` and `rel=noreferrer noopener`;
- a non-GitHub URL renders as text, not a link;
- the filter changes the request's `state` query and the URL;
- the loading, error and empty states.
