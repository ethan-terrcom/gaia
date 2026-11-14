# 13 — Event Bus & SSE

## Goal
Live, replayable updates from runs and agents to the UI.

## Out of scope
UI rendering (17), and any distributed bus such as Redis (that comes with the cloud move).

## Files
- `gaia/services/events.py`
- `gaia/api/routes/stream.py`

## Tasks
1. `EventBus`:
   - `publish(event)`: redact (02), truncate payload fields over 8 KB, append to the `event` table,
     then fan out to in-memory subscribers (`asyncio.Queue` per subscriber, bounded at 1000;
     drop the oldest on overflow and send a `stream.gap` marker).
   - `subscribe(workspace_id, run_id | None)` → an async iterator.
   - Event types (an enum): `run.*`, `step.*`, `agent.message|tool_call|tool_result|usage`,
     `approval.requested|decided`, `pr.*`, `ci.*`, `worktree.*`.
2. An `EventSink` adapter so runtimes (10) and nodes (12) can publish without importing FastAPI.
3. SSE endpoints:
   - `POST /api/v1/stream/ticket` (bearer auth) → `{ticket}`, single-use and valid for 60s.
     This avoids putting the long-lived token in a URL.
   - `GET /api/v1/stream?ticket=...&run_id=...` → `text/event-stream`. The SSE `id:` is `event.seq`,
     `Last-Event-ID` replays from the DB, and a heartbeat comment goes out every 15s.
4. `GET /api/v1/runs/{id}/events?after=&limit=` for paginated history.

## Tests
- Publish → subscriber receives it. Reconnect with `Last-Event-ID` → replays the missed events.
- A secret in a payload is redacted both in the stored row and in the stream.
- A ticket can be used once, and expires.

## Acceptance criteria
- [ ] `curl -N` on the stream shows live events while a fake run executes.

## Decisions and deviations (as built)
- The SSE body is written as the standalone async generator `sse_frames(...)` so tests can
  drive it without `httpx.ASGITransport` buffering an endless response. A test-only module
  constant `SSE_MAX_EVENTS` is passed through the generator for the one HTTP-level SSE test.
- `Last-Event-ID` header wins over `last_event_id` query, as decided in the handoff. The
  query form exists because the ticket is single-use and a browser `EventSource` cannot
  auto-reconnect with a new header.
- The overflow gap marker is kept outside the bounded `asyncio.Queue` as a single
  `missed_from` integer, and yielded before queued events. This keeps the newest
  `queue_size` events in the queue and collapses repeated overflows into exactly one gap
  marker. (After the lead review, `sse_frames` heals the gap from the DB instead of forwarding it.)
- `EventRecord.seq` and `EventRecord.id` are nullable so the same frozen model can represent
  the transport-only `stream.gap` marker, which is never persisted.
- `StreamTickets` imports `Principal` from `gaia.api.auth`; this is a deliberate service→api
  dependency for the ticket principal, because the service is only used by the API layer.
- `GET /api/v1/runs/{run_id}/events` lives in `gaia/api/routes/stream.py` for now; doc 14 may
  move or extend it.
- The dev fake-run route creates repository/ticket/run rows in their own committed
  transaction before starting the background publisher, so event inserts always see a
  committed parent run.

### Lead review changes (2026-11-14, [`review-13`](../reviews/2026-11-14-review-13.md))
- **E1:** a stream opened without `Last-Event-ID` or `?last_event_id=` is **live only**. Replays are
  paged (500 rows per query) instead of loading every event after the resume point at once. History
  is `GET /runs/{id}/events`.
- **E2:** `sse_frames` heals a `stream.gap` by replaying the missed range from the database, so a
  slow client still receives every event exactly once, in order. The client never sees
  `stream.gap`.
- **E3:** stream tickets are **not** registered with the redaction layer. Its registry is never
  pruned and is scanned on every log line.
- Acceptance verified live: `curl -N` on the stream showed 7 events from `gaia dev fake-run` as they
  were published. The API token appeared 0 times in the server log.

- [x] Acceptance: `curl -N` on the stream shows live events while a fake run executes (lead, 2026-11-14).
