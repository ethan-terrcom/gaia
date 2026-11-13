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
   - `GET /api/v1/stream?ticket=...&run_id=...` → `text/event-stream`. Supports `Last-Event-ID`
     for replay from the DB, and sends a heartbeat comment every 15s.
4. `GET /api/v1/runs/{id}/events?after=&limit=` for paginated history.

## Tests
- Publish → subscriber receives it. Reconnect with `Last-Event-ID` → replays the missed events.
- A secret in a payload is redacted both in the stored row and in the stream.
- A ticket can be used once, and expires.

## Acceptance criteria
- [ ] `curl -N` on the stream shows live events while a fake run executes.
