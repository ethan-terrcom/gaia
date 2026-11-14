# Handoff: 13 — Event bus & SSE

Filled from `HANDOFF_TEMPLATE.md` for the lead's sandboxed DeepSeek harness (2026-11-14). File tools
are confined to the worktree. The `run` tool is sandboxed: no network, no git writes, no installs. The
lead does the commit and merge, and the report goes through `finish`.

---

You are a junior engineer on **Gaia**, a local-first control plane for AI coding agents. Your task is
to implement exactly one work package, **13 — Event bus & SSE**. The lead engineer reviews your work,
then commits and merges it. You never commit.

## 1. Read first, in this order
1. `docs/implementation/13-events-and-sse.md`: your contract.
2. `docs/implementation/00-README.md`, "Rules for every work package".
3. `docs/ARCHITECTURE.md` §4–§7.
4. `docs/backend/README.md`, then `docs/backend/api.md`, `database.md` and `python-concepts.md`.
5. `docs/reviews/2026-11-13-code-review-01-05.md`, finding F9 (`event.seq` ordering).
6. The code you build on:
   - `gaia/db/{models,repo}.py` (`Event`, `EventRepo`);
   - `gaia/security/redaction.py`;
   - `gaia/api/{app,deps,errors,auth}.py`, `gaia/api/routes/*.py`;
   - `gaia/domain/{enums,schemas}.py`;
   - `tests/conftest.py`, `tests/test_api_*.py`.

## 2. Lead notes for this package
These are design decisions already made. Follow them, and record any deviation.

**Event types:** add `EventType(StrEnum)` to `gaia/domain/enums.py`, covering at least:
- runs: `run.created`, `run.status_changed`, `run.node_started`, `run.node_finished`,
  `run.budget_exceeded`, `run.failed`;
- steps: `step.started`, `step.finished`;
- agents: `agent.message`, `agent.tool_call`, `agent.tool_result`, `agent.usage`;
- approvals: `approval.requested`, `approval.decided`;
- PRs and CI: `pr.opened`, `pr.updated`, `pr.merged`, `ci.status`;
- worktrees: `worktree.created`, `worktree.removed`;
- the transport-only marker `stream.gap`, which is never persisted.

**`gaia/services/events.py`.**
- `EventBus(sessions: async_sessionmaker[AsyncSession], *, queue_size=1000, max_field_bytes=8192)`
  is built once in the lifespan and stored on `AppServices` as `events`.
- `async publish(*, workspace_id, type, payload, run_id=None, agent_id=None) -> EventRecord`.
  - **Redact** first. Make the existing `_redact_value` public as `redact_value` in
    `gaia/security/redaction.py`, keeping the old name as an alias so nothing else breaks.
  - Then **truncate**: any string field longer than 8 KB (UTF-8 bytes) is cut, and the payload gains
    `"_truncated": [<dotted paths>]`. This is recursive into dicts and lists.
  - Then **insert** in the bus's **own short transaction**: open a session, append through
    `EventRepo`, and commit. Events record what happened even if the caller later rolls back; write
    that in the module docstring.
  - Then **fan out**.
  - Hold one `asyncio.Lock` around insert, commit and fan-out, so subscribers see events in `seq`
    order (F9: on SQLite this is the order of commit).
  - `EventRecord` is a frozen Pydantic model: `seq`, `id`, `workspace_id`, `run_id`, `agent_id`,
    `type`, `payload`, `ts`. This is also the wire format.
- `subscribe(workspace_id, run_id: str | None) -> Subscription` is an async context manager. It
  yields an async iterator of `EventRecord`s and unsubscribes on exit.
  - Each subscription has a bounded `asyncio.Queue(maxsize=queue_size)`.
  - On overflow, drop the **oldest** item and enqueue a `stream.gap` marker. Use `seq=None`, and
    record in the payload the first dropped `seq`, as `missed_from`. Collapse consecutive gaps into
    one marker.
  - A subscriber only ever receives its own workspace's events, and with `run_id` set, only that
    run's.
- `async replay(workspace_id, run_id, after_seq, limit) -> list[EventRecord]` reads from the DB
  through `EventRepo`. Add a method there if needed; it must be ordered by `seq` and scoped by
  workspace.
- **`EventSink`** is a `typing.Protocol` with
  `async def emit(self, type: EventType, payload: Mapping[str, Any]) -> None`.
  `BusEventSink(bus, *, workspace_id, run_id=None, agent_id=None)` implements it. Runtimes (doc 10)
  and graph nodes (doc 12) depend only on the protocol; doc 10 imports it from here.

**Stream tickets (`gaia/services/stream_tickets.py`, in memory).**
- `StreamTickets(ttl_s=60)` lives on `AppServices` as `stream_tickets`.
- `issue(principal) -> str` returns `secrets.token_urlsafe(32)`. Store only its SHA-256, with the
  principal and the expiry, and call `register_secret` on the plaintext.
- `redeem(ticket) -> Principal | None` is **single use**: delete the entry on the first redeem,
  successful or expired. Purge expired entries on every call. Take the clock as an injectable
  `now: Callable[[], float]` so tests do not sleep.

**Routes (`gaia/api/routes/stream.py`).**
- `POST /api/v1/stream/ticket` sits on the authenticated router and returns `{ticket, expires_in}`.
- `GET /api/v1/stream?ticket=...&run_id=...&last_event_id=...` must be mounted **outside**
  `api_v1_router()`, because that router demands a bearer token and `EventSource` cannot send
  headers.
  - Give `stream.py` a second router, `public_router`, and include it in `create_app` next to the
    health router, with the full path `/api/v1/stream`.
  - Its auth is the ticket alone: a missing, used or expired ticket gets 401 in the normal error
    envelope.
  - If `run_id` is given, load it through `ScopedRepo(Run).get` in the ticket's workspace; otherwise
    return 404.
- **Resume point:** the `Last-Event-ID` header wins over the `last_event_id` query parameter. The
  query form exists because the ticket is single use, so a browser `EventSource` cannot simply
  auto-reconnect. The UI (doc 17) fetches a fresh ticket and reconnects with `?last_event_id=`. Put
  this in the route docstring and in the doc's deviations.
- **Ordering without loss or duplicates:** subscribe **first**, then replay from the DB everything
  after `last_seq`, then drain the live queue, skipping any `seq <= ` the last replayed `seq`.
- **Wire format:** `id: <seq>`, `event: <type>`, then `data: <EventRecord JSON>` and a blank line.
  - A `stream.gap` frame has no `id:` line.
  - A heartbeat comment `: ping` goes out every 15 s. Make the interval a module constant the tests
    can override.
  - Headers: `Content-Type: text/event-stream`, `Cache-Control: no-cache` (set explicitly; the
    security middleware uses `setdefault`) and `X-Accel-Buffering: no`.
- Write the body as a standalone async generator,
  `sse_frames(bus, *, workspace_id, run_id, last_seq, heartbeat_s, is_disconnected) -> AsyncIterator[str]`,
  so tests can drive it directly. `httpx.ASGITransport` does **not** stream a response
  incrementally: it buffers the whole body, so an endless SSE response would hang a test. For the
  one HTTP-level test, add a test-only bound (a `max_events` or `stop` hook passed through the
  generator) so the response ends.
- `GET /api/v1/runs/{run_id}/events?after=<seq>&limit=<1..500, default 100>` sits on the
  authenticated router. It returns `{items: [EventRecord], next_after: int | None}`. The run is
  loaded through `ScopedRepo`, and a run in another workspace gets 404. This is the only `runs` route
  in this package; doc 14 adds the rest. Put it in `gaia/api/routes/stream.py`, or in a
  `gaia/api/routes/runs.py` with a docstring that doc 14 extends it.
- **Dev publisher for the acceptance criterion.** Live fan-out needs the server's own bus, so a script
  outside the server cannot publish in-process.
  - Add `POST /api/v1/dev/fake-run` in `gaia/api/routes/dev.py`. It is authenticated and is
    registered **only** when `settings.dev_routes` is true (new field, default `false`).
  - The route creates a scratch repository, ticket and run in the caller's workspace if the body
    gives none, then publishes a scripted sequence in a background task held on `AppServices`. The
    sequence is `run.status_changed`, a few `agent.message`, an `agent.tool_call`, an
    `agent.tool_result` and `step.finished`, at `interval_s`.
  - It returns `{run_id}` at once.
  - Add a matching `gaia dev fake-run [--events N] [--interval S]` to `gaia/cli.py`. It reads the API
    token from the keychain (`ensure_api_token`/`SecretStore`, as `gaia serve` does), posts with
    `httpx` to `http://{settings.host}:{settings.port}`, and prints the run id. It never prints the
    token.
  - The lead runs `GAIA_DEV_ROUTES=true gaia serve`, then `curl -N` on the stream, then
    `gaia dev fake-run`.

## 3. What you may and must not touch
**You may create or edit only:**
- `backend/gaia/services/{events,stream_tickets}.py`;
- `backend/gaia/api/routes/{stream,runs,dev}.py` and `backend/gaia/api/{app,deps,errors}.py`;
- `backend/gaia/domain/{enums,schemas}.py`;
- `backend/gaia/db/repo.py`;
- `backend/gaia/security/redaction.py` (only the public `redact_value` alias);
- `backend/gaia/config/settings.py` (only `dev_routes`);
- `backend/gaia/cli.py` (only `gaia dev fake-run`);
- `backend/tests/**`;
- `frontend/src/api/openapi.json`, regenerated with `uv run python scripts/export_openapi.py`;
- a "Decisions and deviations (as built)" section at the end of
  `docs/implementation/13-events-and-sse.md`.

**You must not:**
- edit other docs, `.github/`, `.pre-commit-config.yaml`, `[tool.*]` in `pyproject.toml`, `uv.lock`,
  `gaia/llm/**`, `gaia/services/{git,proc,worktrees}.py` or existing migrations;
- add dependencies (no `sse-starlette`: write the frames yourself);
- add suppressions without a reason comment;
- build the runs and approvals API (14) or the UI (17).

## 4. Non-negotiable rules
- **Secrets:** a registered secret placed in a payload must come back as `[REDACTED]`, both in the
  stored row and in the frame. Never put the ticket or the bearer token in a log line or event.
- **SQL:** SQLAlchemy expressions only, inside `gaia/db/`, always workspace-scoped.
- **Async hygiene:** no blocking calls on the event loop. Cancel and await background tasks on
  shutdown (the lifespan's `finally`). Tests must not leave tasks pending: warnings are errors.
- **Library APIs:** confirm against the installed source under
  `backend/.venv/lib/python3.12/site-packages/` (Starlette `StreamingResponse`,
  `Request.is_disconnected`; FastAPI `Header`/`Query`).

## 5. How to work
1. Plan first: the files, the public API with signatures, and how each test and the acceptance
   criterion are proven.
2. Build the bus with its tests, then the tickets, then the routes, then the dev route and CLI.
3. Required tests:
   - publish → subscriber receives;
   - a workspace and run filter;
   - an overflow produces exactly one gap marker and keeps the newest 1000;
   - reconnect with `Last-Event-ID` (and with the query form) replays the missed events in order
     with no duplicates, including an event published between subscribe and replay;
   - redaction and truncation in both the row and the frame;
   - a ticket is single use and expires;
   - `/api/v1/stream` without a ticket gets 401, and a bearer token alone is not accepted there;
   - the paginated history and a cross-workspace 404;
   - the dev route is absent when `dev_routes` is false.
4. Before you finish, all of these must pass, run from `backend/`: `uv run ruff check .`,
   `uv run ruff format --check .`, `uv run mypy` and `uv run pytest --cov -q` (100% coverage of
   `gaia/`).
5. Write the "Decisions and deviations (as built)" section.

## 6. When blocked
Stop and put the question under **Open questions** in your report, with what you have so far.

## 7. Report (the `finish` tool's `report` argument), in markdown
```
## Summary
## Plan vs. result
## Tests in the doc   (each, with the test name that proves it)
## Endpoints   (method, path, auth, request, response, error codes)
## Deviations from the doc
## External APIs used   (library@version, API, source file checked)
## Security-relevant changes
## Tests added
## TODOs left for other packages
## Open questions
## Final check output   (last lines of ruff, mypy, pytest --cov)
```
