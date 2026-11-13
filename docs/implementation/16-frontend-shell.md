# 16 — Frontend Shell

## Goal
The UI foundation: routing, an authenticated API client with generated types, the SSE
hook and the layout.

## Out of scope
The feature views (17).

## Tasks
1. Dependencies: `react-router-dom`, `@tanstack/react-query`, `tailwindcss`, `openapi-typescript`,
   `zod` (light runtime checks). Pin exact versions.
2. `npm run gen:api` → `openapi-typescript src/api/openapi.json -o src/api/schema.d.ts`.
   Write a small typed `apiFetch<Path, Method>` wrapper that adds the bearer token, parses the error
   envelope, and sends a 401 to the "connect" screen.
3. Token bootstrap: on load, read `#token=` from the URL, store it in `sessionStorage`, then call
   `history.replaceState` to remove it. Without a token, show a "Run `gaia ui` to connect" screen.
   Never use `localStorage` for the token.
4. `useEventStream({runId?})`: fetch a stream ticket (13), open an `EventSource`, reconnect with
   backoff, and feed events into React Query cache invalidations plus a local ring buffer for live logs.
5. Layout: a left nav (Dashboard, Tickets, Runs, Approvals (with a pending-count badge), PRs, Worktrees,
   Settings), a top bar with today's spend versus the budget, and a dark/light theme.
6. Keyboard: `g d`, `g t`, `g r`, `g a` to switch sections, and `/` to focus search. (The owner wants to
   "move between" things fast.)
7. Content Security Policy in `index.html` via a meta tag: `default-src 'self'; connect-src 'self' http://127.0.0.1:<port>`.
   No `dangerouslySetInnerHTML`. Agent output is rendered as text or sanitized markdown (`react-markdown`
   without raw HTML).

## Tests
- Vitest: token bootstrap strips the hash; the API client maps a 401 correctly.

## Acceptance criteria
- [ ] `make dev-frontend` plus `gaia ui` opens an authenticated shell that shows the pending-approvals badge.
