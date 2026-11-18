# Review: frontend polish, live runs, ticket delete, PR desk

**Branch:** `feat/frontend-polish-live-runs`
**Date:** 2026-11-17

## What landed

1. **Visual system** — IBM Plex, design tokens, `lucide-react`, shared `ui/` primitives, polished shell (nav icons, wider search, icon theme toggle).
2. **Live runs (frontend-only)** — chat-style timeline, tool cards, usage/`_truncated` summaries, diff auto-refresh while a run is active and on write-like tool events.
3. **Ticket delete** — `DELETE /api/v1/tickets/{id}` with **409** on active runs, audit `ticket.delete`, confirm UI on board/table/edit.
4. **PR desk** — `GET /api/v1/pull-requests/{id}` plus `/prs/:id` with embedded run diff, CI/review rollups, pending gate deep-links; list rows open the desk; GitHub remains an escape hatch.

## Decisions and deviations

- No new EventType / reasoning stream: thought process is existing `agent.message` + tools, presented better.
- PR desk does not call GitHub for per-check detail; rollups from the DB are enough for this pass.
- Ticket delete is hard-delete with cascade (schema already did); soft-delete was declined.
- Worktree checkout for this branch was blocked in the agent sandbox (`.git` writes); implementation continued on the feature branch in the main checkout.

## Checks

- Frontend: typecheck + Vitest for touched views.
- Backend: focused ticket delete / PR detail / audit evidence tests.
- Full `make check` should be run before merge (git-fixture tests need unrestricted sandbox).
