# Handoff: 16 — Frontend shell

Filled from `HANDOFF_TEMPLATE.md` for the lead's sandboxed DeepSeek harness (2026-11-14). Your file
tools are confined to the worktree. Your `run` tool is sandboxed: no network, no git writes, no
installs. You can run `uv run …` from `backend/`, and `npm run
{lint,format,typecheck,test,build,gen:api}` with `cwd` set to `frontend`. The lead commits and
merges; you never commit. Your report goes through `finish`.

---

You are a junior engineer on **Gaia**, a local-first control plane for AI coding agents. Your task
is to implement exactly one work package, **16**: the UI foundation. That covers routing, an
authenticated typed API client, the token bootstrap, the live event stream hook, the layout with
nav, the top bar and the theme, keyboard navigation, the CSP, and three small backend pieces the
shell needs. The feature views are **17a–f**; each section here gets only a placeholder page.

## 1. Read first, in this order
1. `docs/implementation/16-frontend-shell.md`: your contract.
2. `docs/implementation/17-frontend-views.md`: what comes next, so your seams fit. Don't build it.
3. `docs/implementation/00-README.md`, "Rules for every work package".
4. `docs/ARCHITECTURE.md` §6 (token handling, same origin, untrusted agent output).
5. `docs/backend/api.md` and `docs/backend/events.md`: the error envelope, auth, stream tickets,
   `last_event_id` and replay.
6. The frontend as it is: `frontend/package.json`, `vite.config.ts`, `index.html`,
   `src/{main,App}.tsx`, `src/App.test.tsx`, `tsconfig*.json`, `.oxlintrc.json`,
   `.prettierrc.json`, `.npmrc`.
7. Backend code you touch or call: `gaia/api/{app,deps,errors,auth}.py`,
   `gaia/api/routes/{stream,agents,tickets,worktrees}.py`, `gaia/domain/schemas.py`
   (`ApprovalRead`), `gaia/services/costs.py` (`daily_llm_usage_cost_usd`, `ensure_budget`),
   `gaia/db/repo.py`, `gaia/config/settings.py`, `gaia/cli.py` (`_ui`), and
   `scripts/export_openapi.py`.

## 2. Lead notes for this package
These are design decisions already made. Follow them, and record any deviation in your report.

### 2.1 Dependencies (already installed by the lead; do not change versions)
- Runtime: `react-router` 8.5.1 (import from `react-router`, never `react-router-dom`),
  `@tanstack/react-query` 5.105.0, `openapi-fetch` 0.17.0, `zod` 4.6.7.
- Dev: `tailwindcss` 4.3.3 and `@tailwindcss/vite` 4.3.3. Tailwind v4 has **no config file**:
  add `tailwindcss()` to `vite.config.ts` plugins and put `@import "tailwindcss";` at the top of
  `src/index.css`. For class-based dark mode, add `@custom-variant dark (&:where(.dark, .dark
  *));` in `index.css`.
- **Type generation** runs from a separate pinned tool folder, `frontend/tools/openapi/`
  (openapi-typescript 7.13.0 with TypeScript 5.9.3), because openapi-typescript needs
  TypeScript 5's compiler API and this app uses TypeScript 7. `npm run gen:api` is wired:
  `src/api/openapi.json` → `src/api/schema.d.ts`. **After you change backend routes**, run:
  1. `uv run python scripts/export_openapi.py` from `backend/`;
  2. `npm run gen:api` from `frontend/`.

  Commit both generated files; the lead does the commit. `schema.d.ts` is in
  `.prettierignore`. Never edit it by hand.
- Check APIs against the installed source in `frontend/node_modules/<pkg>/` (`.d.ts` files and
  READMEs), not from memory. In particular:
  - `openapi-fetch`'s `createClient<paths>()` and `client.use(middleware)` with `onRequest` and
    `onResponse` (`node_modules/openapi-fetch/dist/index.d.ts`);
  - react-router 8's `createBrowserRouter` and `RouterProvider` (read
    `node_modules/react-router/dist/development/index.d.ts` for what v8 exports);
  - React Query 5's `QueryClient`, `useQuery({queryKey, queryFn})` and `invalidateQueries({queryKey})`.

### 2.2 Backend pieces (small; tests in `backend/tests/`, 100% coverage)
1. **`GET /api/v1/approvals?status=&run_id=&limit=`** in a new `gaia/api/routes/approvals.py`.
   - Read-only: the inbox list from doc 14's table, built here because the nav badge needs it.
     `POST /approvals/{id}/decide` stays **14**: leave `# TODO(gaia-14)`.
   - `status` is an `ApprovalStatus | None`; `limit` is 1–200, default 100.
   - Results are **oldest first**, workspace-scoped through `ScopedRepo`, and returned as
     `ApprovalPage{items: list[ApprovalRead], total_pending: int}`. `total_pending` is the
     workspace's pending count whatever the filters, so the badge needs one call.
   - Add the query to `gaia/db/repo.py` if `ScopedRepo` cannot filter and order as needed.
     Follow its existing patterns; SQL stays in `gaia/db/`.
2. **`GET /api/v1/costs/today`** in a new `gaia/api/routes/costs.py`. It returns
   `TodayCosts{spent_usd: Decimal, budget_usd: Decimal, by_provider: list[ProviderCost{provider:
   str, cost_usd: Decimal}], day_start: datetime}`, for the UTC day.
   - Reuse `daily_llm_usage_cost_usd`.
   - Add a `daily_llm_usage_by_provider(session, workspace_id, *, start, end)` beside it in
     `gaia/db/repo.py` for the breakdown.
   - `budget_usd` comes from `settings.daily_budget_usd`. The clock is injectable: use whatever
     the gateway uses for "now"; check how `ensure_budget` gets `now`.
3. **Serving the built UI** (task 7): `gaia/api/spa.py` with `mount_spa(app, dist_dir: Path)`,
   called from `create_app` only when `settings.ui_dist_dir` is set and contains `index.html`.
   - Add `Settings.ui_dist_dir: Path | None = None`.
   - Serve `dist/assets/*` with `StaticFiles`, mounted at `/assets`.
   - Any other `GET` that does not start with `/api/` returns `index.html` with the headers
     `Content-Security-Policy: <the policy in 2.6>`, `Cache-Control: no-store`,
     `X-Content-Type-Options: nosniff` and `Referrer-Policy: no-referrer`. That is the SPA
     fallback. `/api/...` paths keep their normal 404 envelope.
   - **Path safety:** resolve requested asset paths with `StaticFiles` only. Never build a file
     path from the URL yourself.
   - When `ui_dist_dir` is set, `ui_origin` should be the API's own origin. Document that in the
     settings docstring; don't change the default.

### 2.3 `src/api/`: client, token and errors
- `src/api/token.ts`:
  - `TOKEN_KEY = "gaia.token"`, plus `getToken()`, `setToken(t)` and `clearToken()`, using
    **`sessionStorage` only**. Never `localStorage`.
  - `bootstrapToken(win: Window = window): string | null`. If `location.hash` holds
    `token=<value>` (parse it with `URLSearchParams(hash.slice(1))`), store it, then
    `win.history.replaceState(null, "", pathname + search)` so the token leaves the address bar
    and history. Return the stored token.
  - Call it once in `main.tsx` **before** rendering.
- `src/api/errors.ts`:
  - `class ApiError extends Error { status; code; requestId }`.
  - `parseErrorEnvelope(status, body: unknown): ApiError` validates the body with zod against
    `{error: {code: string, message: string, request_id?: string}}`, the envelope from
    `gaia/api/errors.py`. Anything else becomes `code: "http_error"` with a generic message.
- `src/api/client.ts`:
  - `export const api = createClient<paths>({ baseUrl: "" })`. URLs are **relative** (`/api/...`);
    never an absolute origin.
  - One middleware:
    - `onRequest` sets `Authorization: Bearer <token>` when a token exists;
    - `onResponse` handles a 401 by calling `clearToken()` and dispatching
      `window.dispatchEvent(new Event("gaia:unauthorized"))`, then turns any non-2xx response
      into a thrown `ApiError` via `parseErrorEnvelope`. Read the body from a `clone()`.
  - Export typed helpers the shell uses: `fetchMe()`, `fetchPendingApprovals()` and
    `fetchTodayCosts()`. Each unwraps `{data}` and throws on error, so React Query sees
    rejections.
  - **Do not hand-write a typed fetch wrapper**; every call goes through `api.GET`/`api.POST`
    with `paths` types.
- `src/api/queryClient.ts`: `makeQueryClient()` with `staleTime: 10_000`. Retry: never for an
  `ApiError` with status < 500; otherwise up to 2 times.

### 2.4 Auth gate and screens
- `src/App.tsx` is the root:
  - with no token, render `<ConnectScreen/>` ("Run `gaia ui` in a terminal and open the link it
    prints." plus a short explainer);
  - listen for `gaia:unauthorized` and switch to `ConnectScreen`;
  - otherwise render the router inside `QueryClientProvider`.
- Routes (`src/routes.tsx`) all live under `AppLayout`:
  - `/` → Dashboard, `/tickets`, `/runs`, `/runs/:id`, `/approvals`, `/prs`, `/worktrees` and
    `/settings`;
  - `*` → a NotFound page.

  Each section is a placeholder component in `src/pages/` that shows its title and "Arrives in
  17x" (17a dashboard, 17b tickets, 17c run detail, 17d approvals, 17e PRs, 17f worktrees). Keep
  each one tiny; 17 replaces them.

### 2.5 Layout, live stream and keyboard
- `src/layout/AppLayout.tsx` has a left nav and a top bar with `<Outlet/>`.
  - **Nav order:** Dashboard, Tickets, Runs, Approvals, PRs, Worktrees, Settings, using
    `NavLink`s.
  - The Approvals item shows a **badge** with `total_pending` from
    `useQuery({queryKey: ["approvals","pending"], queryFn: fetchPendingApprovals})`. Hide the
    badge at 0, and cap the label at `99+`. It has an `aria-label` such as "3 pending approvals".
  - **Top bar:** today's spend against the budget, from `["costs","today"]`, formatted `$1.23 /
    $20.00`. Add a small bar that turns amber at ≥80% and red at ≥100%, and a title tooltip with
    the per-provider breakdown. Decimals arrive as **strings** in JSON; parse them with
    `Number()` for display only.
  - **Theme toggle:** light, dark or system. Store the preference in `localStorage` under
    `gaia.theme`; that is a UI preference, not a secret, so it is allowed. Apply the `dark` class
    on `<html>`.
  - Show `/me`'s user name somewhere small.
- `src/hooks/useEventStream.ts`: `useEventStream({ runId }: { runId?: string } = {})` returns
  `{ events: GaiaEvent[], status: "connecting"|"open"|"reconnecting"|"closed" }`.
  - **Connect:** `POST /api/v1/stream/ticket` through `api` gives a `ticket`. Then open
    `new EventSource(`/api/v1/stream?ticket=${t}&run_id=…&last_event_id=…`)`, adding `run_id`
    and `last_event_id` only when known. Build the query with `URLSearchParams`.
  - Tickets are single-use, so **never let EventSource auto-reconnect**. On `error`, close it and
    reconnect yourself with a **new ticket**, passing the last seen numeric event id as
    `last_event_id`.
  - **Backoff:** 1, 2, 4, 8, 16, then 30 s maximum, with ±20% jitter. Reset it after an `open`
    that lasts 30 s or receives an event. Getting a ticket can return 401; that goes through the
    middleware, so stop on it.
  - **Parse** `MessageEvent.data` as JSON and validate it with a zod schema of what
    `gaia/services/events.py` serialises: read it, at least `id`/`seq`, `type`, `run_id`,
    `payload` and `ts`. Drop invalid events, with a `console.warn` in dev only. The `event:` name
    and `id:` line come from the SSE framing; check how the server frames them in
    `routes/stream.py` before choosing between `onmessage` and named listeners. Handle
    `stream.gap` by invalidating everything for the run.
  - **Ring buffer:** keep the last 500 events in state.
  - **Invalidation map** (export it as `INVALIDATIONS` so it is testable):
    - `run.*` and `step.*` → `["runs"]`, plus `["runs", runId]` when the event has a run;
    - `approval.*` → `["approvals"]`;
    - `pr.*` and `ci.*` → `["pull-requests"]`;
    - `worktree.*` → `["worktrees"]`;
    - `agent.usage` and `run.budget_exceeded` → `["costs"]`.

    Call `queryClient.invalidateQueries({queryKey})` for each.
  - Inject `EventSource` and the ticket fetcher through an optional second argument or a context,
    so tests use fakes. jsdom has no `EventSource`.
  - `AppLayout` mounts one global `useEventStream()` so badges and spend stay live.
- `src/hooks/useHotkeys.ts`:
  - `g` followed within 1 s by `d`, `t`, `r`, `a`, `p`, `w` or `s` navigates to Dashboard,
    Tickets, Runs, Approvals, PRs, Worktrees or Settings;
  - `/` focuses the first element with `data-search-input` and calls `preventDefault`;
  - keys are ignored while focus is in an `input`, a `textarea`, a `select` or a
    `contenteditable`, and when Ctrl, Meta or Alt is held;
  - show a hint list in the Settings placeholder.

### 2.6 CSP and untrusted text
- **Policy:** `default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:;
  connect-src 'self'; font-src 'self'; object-src 'none'; base-uri 'none'; form-action 'self';
  frame-ancestors 'none'`.
- Put it in a `<meta http-equiv="Content-Security-Policy">` **only in production builds**. Vite's
  dev server and React refresh inject inline scripts that this policy would block. Add a tiny
  Vite plugin in `vite.config.ts` with `apply: "build"` and `transformIndexHtml` that inserts the
  meta tag. The SPA route in 2.2 also sends it as a header. Define the policy string **once** in
  a shared constant: `frontend/csp.json` is read by both `vite.config.ts` and the backend test
  that compares them. If that is awkward, keep two copies and add a backend test that asserts the
  copies are equal.
- No `dangerouslySetInnerHTML` anywhere. Render agent and repo text as plain text. The
  `react-markdown` renderer is 17's concern; don't add it.

### 2.7 Vite dev proxy (task 7)
`server.proxy: { "/api": { target: "http://127.0.0.1:8765", changeOrigin: false } }`. Port 8765
is the backend default (`Settings.port`). SSE works through Vite's proxy; don't buffer.

### 2.8 `gaia ui`
Keep printing the URL; do not add `--open`. Doc 04's reason still stands. Change the
`TODO(gaia-16)` comment in `cli.py` to say the login flow is now `#token=` stripped by
`bootstrapToken`, and that `--open` stays out because a token must not appear in a process's argv.

### 2.9 Tests (Vitest with jsdom and Testing Library; the existing `App.test.tsx` shows the setup)
- `bootstrapToken`: it stores the hash token in `sessionStorage`, strips the hash with
  `replaceState`, and never touches `localStorage`. Spy on `Storage.prototype.setItem`, and
  check `this === localStorage`.
- Client middleware: the bearer header is added; a 401 clears the token and fires
  `gaia:unauthorized`; a 422 envelope becomes an `ApiError` with its code and message; a non-JSON
  500 becomes a generic `ApiError`. Stub `fetch` with `vi.stubGlobal` or pass a custom `fetch` to
  `createClient` (check the option name).
- `App`: no token shows the connect screen; a token renders the layout; `gaia:unauthorized`
  goes back to connect.
- Badge: hidden at 0, shows `3`, shows `99+`.
- `useEventStream` with a fake EventSource:
  - it connects with a fresh ticket;
  - an error leads to a reconnect with a **new** ticket and the `last_event_id`, using fake
    timers for the backoff;
  - an invalid payload is dropped;
  - the ring buffer is capped at 500;
  - the invalidation map calls `invalidateQueries` with the right keys.
- Hotkeys: `g` then `a` navigates to `/approvals`; it is ignored inside an input; `/` focuses
  search.
- The top bar formats spend and changes colour at 80% and 100%.
- Backend: approvals list (ordering, status filter, workspace scoping, `total_pending`);
  costs/today (sum and breakdown, with rows from yesterday excluded); SPA (index fallback with
  the CSP header, `/api/nope` still the JSON 404, an asset is served, a traversal attempt such as
  `/assets/../../etc/passwd` does not escape, and it is not mounted when `ui_dist_dir` is unset).

## 3. Files
**You may create or edit:**
- under `frontend/`: everything in `src/` except `src/api/schema.d.ts` and
  `src/api/openapi.json`, which are generated (run the generators); `vite.config.ts`,
  `index.html`, `src/index.css`, `frontend/csp.json`; `tsconfig.app.json`, but only if a
  `paths`/`types` addition is needed, with a reason;
- under `backend/`: `gaia/api/routes/approvals.py`, `gaia/api/routes/costs.py`,
  `gaia/api/spa.py`, `gaia/api/app.py` (router includes and the SPA mount), `gaia/db/repo.py`
  (the two queries), `gaia/domain/schemas.py` (`ApprovalPage`, `TodayCosts`, `ProviderCost`),
  `gaia/config/settings.py` (`ui_dist_dir`), `gaia/cli.py` (the comment only), and tests;
- `docs/implementation/16-frontend-shell.md`: add "Decisions and deviations (as built)" at the end.

**You must not touch:**
- `frontend/package.json`, `package-lock.json`, `frontend/tools/`, `.npmrc`, `.oxlintrc.json` and
  `.prettierrc.json`. Ask in the report if you think a rule or script must change;
- `backend/pyproject.toml`, `uv.lock` and every `[tool.*]` section;
- `.github/`, migrations, other docs.

## 4. Non-negotiable rules
- The bearer token lives only in `sessionStorage` and in the `Authorization` header. It never
  goes in a URL, except the one-time `#token=` that you strip; never in `localStorage`, a log or
  an error message. The SSE uses a **ticket**, never the token.
- Same origin only: relative `/api/...` URLs, and no CORS changes on the backend.
- No `dangerouslySetInnerHTML`, no `eval`, and no inline `<script>` in the built HTML.
- TypeScript strict as configured; no `any` without a comment saying why. `npm run lint` uses
  `--deny-warnings`.
- Backend: mypy strict, SQL only in `gaia/db/`, every route authenticated except the SPA
  fallback and static assets, which carry no data.

## 5. How to work
1. Read §1, then write a short plan as your first message.
2. Order: backend routes → export openapi → `gen:api` → token/errors/client → App gate and routes
   → layout, badge and top bar → `useEventStream` → hotkeys → CSP plugin → SPA serving.
3. Checks, all of which must pass:
   - from `frontend/` (`cwd: "frontend"`): `npm run lint`, `npm run typecheck`, `npm run test`,
     `npm run build`;
   - from `backend/`: `uv run ruff check .`, `uv run ruff format --check .`, `uv run mypy`,
     `uv run pytest -q` (100% coverage).
4. Prettier is strict (`lint` runs `prettier --check`). Run `npm run format` before the final
   checks.

## 6. When blocked
Do not guess at architecture. Write the question under "Open questions", choose the most
restrictive reasonable behaviour, and keep going.

## 7. Report (pass to `finish`, in Markdown, with these headings)
- **Summary**
- **Files changed**
- **Deviations**, each with a reason
- **External APIs used**: library calls, with the installed `.d.ts` file you checked
- **Security-relevant changes**: token handling, CSP, SPA serving, auth on the new routes
- **Tests added**
- **Open questions**
- **Final check output**: the last lines of each of the eight checks
