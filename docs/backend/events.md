# Events and the live stream

> Package **13** · `gaia/services/events.py`, `gaia/services/stream_tickets.py`,
> `gaia/api/routes/{stream,dev}.py` · current as of 2026-11-14

## What it's for
Everything that happens in a run is published as an **event**: a status change, an agent message,
a tool call, an approval request. Events go to two places at once:
- the `event` table, a durable, append-only log;
- every browser tab watching that run, over **Server-Sent Events** (SSE).

The UI never polls. It opens one stream, and on reconnect it asks for "everything after event
*N*".

## Files
| File | Role |
|---|---|
| `services/events.py` | `EventBus` (publish, subscribe, replay), `EventRecord` (the wire model), `EventSink` / `BusEventSink` |
| `services/stream_tickets.py` | single-use, 60-second tickets that let a browser open the stream without putting the API token in a URL |
| `api/routes/stream.py` | `POST /stream/ticket`, `GET /stream` (SSE), `GET /runs/{id}/events` (paginated history) |
| `api/routes/dev.py` | `POST /dev/fake-run`, mounted only when `GAIA_DEV_ROUTES=true` |
| `domain/enums.py` | `EventType`: `run.*`, `step.*`, `agent.*`, `approval.*`, `pr.*`, `ci.*`, `worktree.*`, `stream.gap` |

## How it works

### Publishing
Code that wants to report something depends on the tiny `EventSink` protocol, never on FastAPI or
the bus:

```python
await sink.emit(EventType.AGENT_TOOL_CALL, {"name": "run_command", "argv": ["pytest", "-q"]})
```

`BusEventSink` fixes the workspace, run and agent, and calls `EventBus.publish`, which:
1. **redacts** every string (registered secrets and token shapes become `[REDACTED]`);
2. **truncates** any string over 8 KB and records which fields were cut, in `_truncated`;
3. **commits** the row in its *own* short transaction, so the trail survives even if the caller
   later rolls back;
4. **fans out** to subscribers' in-memory queues.

Steps 3 and 4 run under one `asyncio.Lock`. That makes `seq` (the table's autoincrement key) the
order every subscriber sees.

### Subscribing, and the "no gaps, no duplicates" rule
A subscription is an `asyncio.Queue` bounded at 1,000 events. The SSE generator `sse_frames()`:
1. **subscribes first**, so nothing published from this moment is missed;
2. if the client gave a resume point (`Last-Event-ID` header, or `?last_event_id=`), **replays**
   from the DB everything after it, 500 rows per query;
3. **drains the live queue**, skipping anything whose `seq` it already sent. That is how an event
   committed between steps 1 and 2 arrives exactly once.

If a client reads too slowly and its queue overflows, the bus drops the oldest queued event and
leaves a `stream.gap` marker. `sse_frames()` handles the marker itself: it replays the missed range
from the DB, so the client still sees every event, once, in order. Without a resume point, a
stream is **live only**. History lives at `GET /runs/{id}/events`.

### Why a ticket, not the token?
`EventSource` (the browser's SSE client) cannot send an `Authorization` header, and a token in a
URL ends up in logs and history. So the UI:
1. sends `POST /api/v1/stream/ticket` with its bearer token and gets back a random ticket, stored
   only as a SHA-256 hash;
2. opens `GET /api/v1/stream?ticket=…&run_id=…`.

The ticket works **once** and expires after 60 s. Because it is single use, the browser cannot
auto-reconnect with the same URL. The UI (doc 17) fetches a new ticket and reconnects with
`?last_event_id=<last seq it saw>`.

The stream route is mounted **outside** the bearer-protected `/api/v1` router, and the ticket is
its only credential. A bearer token alone gets 401 there.

## Wire format
```
id: 42
event: agent.tool_call
data: {"seq":42,"id":"…","workspace_id":"…","run_id":"…","agent_id":null,"type":"agent.tool_call","payload":{…},"ts":"…"}

: ping
```
A `: ping` comment goes out every 15 s, so proxies and the browser keep the connection open.

## Try it
```sh
GAIA_DATA_DIR=/tmp/g GAIA_DEV_ROUTES=true uv run gaia serve &
TICKET=$(curl -s -X POST -H "Authorization: Bearer $TOKEN" localhost:8765/api/v1/stream/ticket | jq -r .ticket)
curl -N "http://127.0.0.1:8765/api/v1/stream?ticket=$TICKET" &
GAIA_DATA_DIR=/tmp/g uv run gaia dev fake-run --events 3
```

## Changing it safely
- **Publish through `EventSink`.** Never write `Event` rows directly: they would skip redaction,
  truncation and fan-out.
- **New event type:** add it to `EventType` and use it. The UI ignores types it doesn't know.
- **Never hold the bus lock across anything slow.** It serializes every publisher in the process.
- **Ordering assumes one process and SQLite's single writer.** A multi-process or Postgres
  deployment needs another ordering scheme, such as a per-run sequence or `LISTEN/NOTIFY`
  (finding F9).
- Tests drive `sse_frames()` directly. `httpx.ASGITransport` buffers whole responses, so an HTTP
  test of an endless stream would hang. The single HTTP-level test sets `SSE_MAX_EVENTS`.

See [Python concepts → async generators and `async with`](python-concepts.md#async-and-await).
