# 16 — Frontend Shell

## Goal
The UI foundation: routing, an authenticated API client with generated types, the SSE
hook and the layout.

## Out of scope
The feature views (17).

## Tasks
1. Dependencies: `react-router` (v7+ ships everything from `react-router`; `react-router-dom` is legacy),
   `@tanstack/react-query`, `tailwindcss` with `@tailwindcss/vite` (Tailwind v4: no config file),
   `openapi-typescript`, `openapi-fetch`, `zod` (light runtime checks). Versions are exact automatically
   (`frontend/.npmrc`).
2. `npm run gen:api` → `openapi-typescript src/api/openapi.json -o src/api/schema.d.ts`.
   Use `openapi-fetch` (`createClient<paths>()`) with a middleware that adds the bearer token, parses
   the error envelope, and sends a 401 to the "connect" screen. Don't hand-write a typed fetch wrapper.
3. Token bootstrap: on load, read `#token=` from the URL, store it in `sessionStorage`, then call
   `history.replaceState` to remove it. Without a token, show a "Run `gaia ui` to connect" screen.
   Never use `localStorage` for the token.
4. `useEventStream({runId?})`: fetch a stream ticket (13), open an `EventSource`, reconnect with
   backoff, and feed events into React Query cache invalidations plus a local ring buffer for live logs.
5. Layout: a left nav (Dashboard, Tickets, Runs, Approvals (with a pending-count badge), PRs, Worktrees,
   Settings), a top bar with today's spend versus the budget, and a dark/light theme.
6. Keyboard: `g d`, `g t`, `g r`, `g a` to switch sections, and `/` to focus search. (The owner wants to
   "move between" things fast.)
7. **Same origin.** In dev, Vite's `server.proxy` forwards `/api` to the backend; in production, FastAPI
   serves `frontend/dist`. The UI only calls relative `/api/...` URLs, so there's no CORS.
8. Content Security Policy in `index.html` via a meta tag: `default-src 'self'` (same origin, so
   `connect-src 'self'` covers the API and SSE).
   No `dangerouslySetInnerHTML`. Agent output is rendered as text or sanitized markdown (`react-markdown`
   without raw HTML).

## Tests
- Vitest: token bootstrap strips the hash; the API client maps a 401 correctly.

## Acceptance criteria
- [ ] `make dev-frontend` plus `gaia ui` opens an authenticated shell that shows the pending-approvals badge.

## Decisions and deviations (as built)
- `GET /api/v1/approvals` is implemented in `gaia/api/routes/approvals.py` with a new
  `ApprovalRepo.list_inbox`/`count_pending` query in `gaia/db/repo.py`. Inbox ordering is
  `(created_at, id)`, not `id` alone, so same-millisecond approvals still read oldest first.
- `GET /api/v1/costs/today` uses a module-level `_now()` clock (the same seam style as the LLM
  gateway's `_now`) so tests can pin the UTC day.
- The SPA fallback is a `/{full_path:path}` route with `include_in_schema=False`, added after the
  API routers, rather than ASGI middleware. This keeps the existing middleware order untouched and
  keeps the generated OpenAPI document free of a catch-all path.
- `mount_spa` requires `dist_dir/assets` to exist and raises otherwise; the task only called for
  mounting when `index.html` exists, but a built Vite app without `assets/` is malformed.
- The CSP string lives in both `gaia/api/spa.py` and `frontend/csp.json`; a backend test asserts
  they stay equal.
- `frontend/tools/openapi/` (lead setup) holds `openapi-typescript` 7.13.0 with TypeScript 5.9.3, apart
  from the app: openapi-typescript needs TypeScript 5's compiler API, and the app uses TypeScript 7,
  which no longer exposes it. `npm run gen:api` and `make openapi` use it. `make setup` installs it.
- `react-router` is v8 (8.5.1), the newest release that was at least two weeks old; the doc said v7+.

### Lead review changes — 16 (2026-11-14, [`review-16`](../reviews/2026-11-14-review-16.md))
- **U1 (high):** the server frames every SSE event with its type (`event: run.created`), and a
  browser delivers named events **only** to `addEventListener(<type>)` listeners, never to
  `onmessage`. The hook listened on `onmessage` only, so in a real browser it would have received
  nothing. The tests' fake EventSource called `onmessage` directly, which hid it.
  - Fix: the hook registers a listener per type from `frontend/src/api/event-types.json`, and
    `backend/tests/test_frontend_contract.py` asserts that file equals the `EventType` enum.
  - The fake EventSource now behaves like a browser.
  - Proved live with Node's WHATWG `EventSource` against a running server: `onmessage` received
    0 of 7 events, and the named listeners received all 7.
- **U2:** `seq` and `id` are nullable in the event schema; the transport-only `stream.gap` marker
  has both `null`, and was dropped.
- **U3:** the nav badge asks for `status=pending&limit=1`; it needs only `total_pending`.
- Verified live, through the backend serving `frontend/dist`:
  - the SPA route sends the CSP, `no-store`, `nosniff` and `no-referrer` headers;
  - assets are served as `text/javascript`;
  - `--path-as-is /assets/../../etc/passwd` returns 404;
  - an unknown `/api/…` path keeps the JSON 404 envelope;
  - the new routes require the token (401 without it).
- [ ] Acceptance ("`make dev-frontend` plus `gaia ui` opens an authenticated shell that shows the
  pending-approvals badge"): the pieces are verified as above. A visual check in a browser is the
  owner's first-run step in the user guide.
