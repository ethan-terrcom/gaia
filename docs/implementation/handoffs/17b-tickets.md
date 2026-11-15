# Handoff: 17b — Tickets

Filled from `HANDOFF_TEMPLATE.md` for the lead's sandboxed DeepSeek harness (2026-11-14). Your file
tools are confined to the worktree. Your `run` tool is sandboxed: no network, no installs. You can
run `npm run …` with `cwd: "frontend"`. The lead commits; your report goes through `finish`.

You are a junior engineer on **Gaia**. Build exactly one frontend view, **17b: Tickets**, at
`/tickets`. It replaces the placeholder in `src/pages/Tickets.tsx`. Read `docs/frontend/README.md` and
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
- **Data:** `GET /api/v1/tickets` (read `schema.d.ts` for its query parameters: filters,
  pagination cursor) and `GET /api/v1/repositories` for the repo filter and form. Query keys
  `['tickets', {…filters}]` and `['repositories']`.
- **Two layouts**, toggled and kept in the URL (`?view=board|table`):
  - **Board:** one column per `TicketStatus` (`backlog`, `ready`, `in_progress`, `in_review`,
    `done`, `cancelled`), with cards showing the title (plain text), the repo and the priority.
    **No drag-and-drop**: status changes go through the edit modal, because the backend owns the
    transitions and may refuse.
  - **Table:** title, repo, status badge, priority, and updated (relative).
- **Filters** (in the URL): a repository select, and a search box (`data-search-input`, so the `/`
  hotkey focuses it) that filters by title client-side over the loaded page, or uses an API `q`
  parameter if `schema.d.ts` has one.
- **Create/edit modal:** title (required), body (a multi-line textarea, plain text), repository
  (create only), priority, and status (edit only).
  - `POST /api/v1/tickets` and `PATCH /api/v1/tickets/{id}`.
  - A 409 or 422 shows the `ApiError` message inline in the modal.
  - On success, invalidate `['tickets']`.
- **Start run:** a button on each ticket card or row (not on `done` or `cancelled`). It opens a
  small dialog with the junior runtime (a `<select>` of `native_deepseek` and `claude_agent_sdk`,
  default "Default (from settings)", sent as omitted) and an optional budget in USD (a number
  > 0; empty means the default).
  - `POST /api/v1/runs {ticket_id, junior_runtime?, budget_usd?}`.
  - On **201**, navigate to `/runs/<id>`. A **409** ("already has an active run") shows the
    message and a link to that run if the response includes it. A **503** says "The orchestrator
    is not running".

## Tests
The board groups by status; the table toggle and URL; the repo filter and search; create (POST
body, inline error); edit (PATCH); start run (the POST body without and with options, navigation
on 201, the 409 and 503 messages); the loading, error and empty states.
