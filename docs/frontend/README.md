# Frontend developer guide

> **Audience:** developers working in `frontend/` who know React and TypeScript, but not
> necessarily React Query, openapi-fetch, Server-Sent Events or Tailwind v4.
>
> **Status:** current as of the 17 views (2026-11-14): the shell (16) and all six feature views
> (17a–f) are built.

## What the frontend is
A single-page React app: the architect's cockpit for tickets, runs, approvals and PRs. It talks
only to Gaia's own API, on the same origin:
- in development, Vite proxies `/api` to the backend on `127.0.0.1:8765`;
- in production, the backend serves the built `frontend/dist` itself.

There is no CORS and no absolute URL anywhere in the app.

## Run it
```sh
make setup            # once: installs backend, frontend and the type-generator tool
make dev-backend      # terminal 1: the API on 127.0.0.1:8765
make dev-frontend     # terminal 2: Vite on 127.0.0.1:5173
cd backend && uv run gaia ui   # prints the one-time login URL; open it in your browser
```

## The views
| Route | View (`src/views/…`) | What it is for |
|---|---|---|
| `/` | `dashboard/` | active runs, what needs you, open PRs, today's spend, the last 14 days |
| `/tickets` | `tickets/` | board and table, create and edit, **Start run** |
| `/runs/:id` | `run-detail/` | one run: header, steps, approvals, live timeline, diff (`?tab=diff`), cancel |
| `/approvals` | `approvals/` | the decision inbox: `j`/`k`, `a`/`r`, a typed `merge <n>` |
| `/prs` | `pull-requests/` | Gaia's PRs with CI and review state |
| `/worktrees` | `worktrees/` | repositories (register, commands) and worktrees (flags, remove) |

Each view owns its queries and passes the current global `fetch` per request, so tests can stub it.
Query keys start with the event stream's invalidation prefixes, so views refresh live.

## Files
| Path | Role |
|---|---|
| `src/main.tsx` | calls `bootstrapToken()` **before** rendering, then mounts `<App/>` |
| `src/App.tsx` | the auth gate: no token → `ConnectScreen`; `gaia:unauthorized` → back to it; otherwise the router inside `QueryClientProvider` |
| `src/routes.tsx` | the routes, all under `AppLayout`; each `src/pages/*.tsx` renders a view from `src/views/` |
| `src/api/token.ts` | the token in `sessionStorage` only; `bootstrapToken` takes `#token=` from the URL and strips it |
| `src/api/client.ts` | `api = createClient<paths>()` (openapi-fetch) with the auth/error middleware; `fetchMe`, `fetchPendingApprovals`, `fetchTodayCosts` |
| `src/api/errors.ts` | `ApiError` and `parseErrorEnvelope`, which zod-validates `{error: {code, message, request_id}}` |
| `src/api/queryClient.ts` | React Query defaults: 10 s stale time, no retry on 4xx |
| `src/api/schema.d.ts` | **generated** types (`npm run gen:api`); never edit by hand |
| `src/api/openapi.json` | **generated** by `make openapi` from the backend |
| `src/api/event-types.json` | every SSE event name; a backend test keeps it equal to `EventType` |
| `src/hooks/useEventStream.ts` | the live event stream: tickets, reconnects, invalidations, a ring buffer |
| `src/hooks/useHotkeys.ts` | `g d/t/r/a/p/w/s` and `/` |
| `src/layout/AppLayout.tsx` | nav with the pending-approvals badge, the top bar with today's spend, the theme toggle |
| `csp.json` | the production Content Security Policy, shared with the backend |
| `tools/openapi/` | a pinned, separate install of `openapi-typescript` + TypeScript 5 (see below) |

## How it works

### Logging in
`gaia ui` prints `http://127.0.0.1:5173/#token=<token>`.
1. On load, `bootstrapToken` reads the hash, stores the token in **`sessionStorage`**, and calls
   `history.replaceState` so it leaves the address bar and the history.
2. The token is never put in `localStorage`, so it does not outlive the tab, and never in a query
   string, so it never reaches a server log or a `Referer` header.
3. Every API call adds `Authorization: Bearer …` in the openapi-fetch middleware.
4. A 401 clears the token and fires `gaia:unauthorized`, and the app shows the connect screen.

### Calling the API
Use the typed client and nothing else:
```ts
const { data } = await api.GET('/api/v1/approvals', {
  params: { query: { status: 'pending', limit: 1 } },
})
```
`paths` comes from the backend's OpenAPI schema, so a wrong path, parameter or response field is a
type error. The middleware turns any non-2xx response into a thrown `ApiError`, which React Query
sees as a failed query. After changing a backend route, run `make openapi`, which exports the
schema and regenerates the types.

### Live updates: `useEventStream`
The backend streams events over SSE (doc 13). One global `useEventStream()` in `AppLayout` keeps
badges and spend live; a run page can open its own with `{ runId }`.
1. **Ticket first:** `POST /api/v1/stream/ticket` returns a single-use, 60-second ticket. The bearer
   token is never put in the SSE URL.
2. **Open:** `new EventSource('/api/v1/stream?ticket=…&run_id=…&last_event_id=…')`.
3. **Named events.** The server sends `event: run.created`, `event: approval.requested`, and so on.
   **A browser delivers a named event only to `addEventListener('<name>')`, never to `onmessage`**,
   so the hook registers one listener per name in `event-types.json`. If you add an `EventType`
   in the backend, regenerate that file; `tests/test_frontend_contract.py` fails until you do.
4. **Reconnect yourself.** A ticket works once, so the browser's built-in reconnect can't. On an
   error the hook closes the stream, waits 1, 2, 4, 8, 16, then 30 s (±20% jitter), gets a **new**
   ticket, and resumes with `last_event_id` set to the last `seq` it saw. The server replays what
   was missed.
5. Each event is zod-validated, kept in a 500-event ring buffer, and mapped to React Query
   invalidations (`INVALIDATIONS`): `run.*` → `['runs']` (and `['runs', id]`), `approval.*` →
   `['approvals']`, `pr.*`/`ci.*` → `['pull-requests']`, `worktree.*` → `['worktrees']`, and
   `agent.usage` → `['costs']`.

### Security rules
- **CSP:** `default-src 'self'` and friends, from `csp.json`. A Vite plugin injects the meta tag in
  production builds only, because the dev server's hot reload uses inline scripts. The backend
  also sends it as a header on the SPA route. A backend test keeps both copies equal.
- **Untrusted text.** Agent output, file contents and ticket text are rendered as text. There is
  no `dangerouslySetInnerHTML` anywhere. 17's sanitized Markdown renderer must not allow raw HTML.

### Styling and theme
Tailwind v4, with no config file: `@import "tailwindcss"` in `src/index.css` and the
`@tailwindcss/vite` plugin. Dark mode is class-based (`@custom-variant dark`). The toggle stores
`light`, `dark` or `system` in `localStorage['gaia.theme']`. That is a preference, not a secret,
which is why it may go there.

### Why the type generator lives in `tools/openapi/`
`openapi-typescript` builds its output with TypeScript's compiler API. TypeScript 7, which the
app uses, is the native rewrite and doesn't expose that API, and npm cannot install two root
TypeScript versions. So the generator has its own small, pinned `package.json` with TypeScript 5.
It is a build tool only; nothing in the app imports it.

### Shared building blocks (`src/components/`)
Every view uses these, so all views behave alike. Doc 17's shared rules require them.

| Component | Use it for |
|---|---|
| `LoadingState`, `ErrorState`, `EmptyState` | the three states every view must handle. `ErrorState` shows an `ApiError`'s message and code, and a generic sentence for anything else, so raw exception text never reaches the screen |
| `PageHeader` | the title row with optional actions |
| `ConfirmDialog` | **every** destructive action. `requireText="merge 7"` makes the user type a phrase first: merge approvals, force-removing a dirty worktree. Escape cancels |
| `StatusBadge` (+ `status.ts`) | any status string (run, PR, CI, review, worktree). Unknown values render neutrally |
| `format.ts` | `formatUsd` (money arrives as decimal strings), `formatRelative`, `shortSha` |

## Checks
`npm run lint` (oxlint with warnings as errors, plus prettier), `npm run typecheck`, `npm test`
(Vitest, jsdom) and `npm run build`. `make check` runs them all with the backend checks.
Tests fake `EventSource` and `fetch`. Keep the fakes **browser-faithful**, because a fake that
calls `onmessage` for named events is exactly what hid U1.

Related: [doc 16](../implementation/16-frontend-shell.md) ·
[review 16](../reviews/2026-11-14-review-16.md) · [events (backend)](../backend/events.md) ·
[API (backend)](../backend/api.md).
