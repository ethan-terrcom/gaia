# API

> Packages 04, 06 · Source of truth: [`04-api-skeleton-auth.md`](../implementation/04-api-skeleton-auth.md), [`06-repositories-and-tickets-api.md`](../implementation/06-repositories-and-tickets-api.md)
> · Concepts used: [dependency injection](python-concepts.md#dependency-injection-in-fastapi),
> [ASGI middleware](python-concepts.md#asgi-middleware), [context managers](python-concepts.md#context-managers-and-async-with),
> [constant-time comparison](python-concepts.md#constant-time-comparison), [Protocols](python-concepts.md#protocols)

## What it's for

The API is how the UI (and `curl`) reaches Gaia. It is built to be safe on a developer machine:
it listens on loopback only, requires a bearer token on every route except the health check,
rejects forged `Host` headers, and returns one predictable error shape that never contains a
traceback or a secret.

## Files

| File | Contents |
|---|---|
| `gaia/api/app.py` | `create_app()`, the lifespan, `SecurityHeadersMiddleware`, `RequestContextMiddleware`, `api_v1_router()` |
| `gaia/api/auth.py` | `Principal`, the `AuthProvider` protocol, `LocalTokenAuth`, `bearer_token()` |
| `gaia/api/deps.py` | `AppServices`, `get_session`, `get_principal`, `Scoped`, and the `SessionDep` / `PrincipalDep` / `ScopedDep` shorthands |
| `gaia/api/errors.py` | The error envelope, `ErrorCode`, exception handlers, request ids |
| `gaia/api/routes/health.py` | `GET /api/health` (no auth) |
| `gaia/api/routes/session.py` | `GET /api/v1/me` |

## How it works

### Startup (the lifespan)

`create_app(settings)` builds the app. When the server starts, the lifespan runs, in this order:

1. configure logging (with redaction);
2. create `~/.gaia` at `0700`;
3. open the database engine and seed the workspace and owner (failing with "run `make migrate`" on an
   empty database);
4. make sure the API token exists in the keychain, generating it on first run;
5. put everything on `app.state.services` (an `AppServices`) for the dependencies to use.

On shutdown it disposes of the engine. `gaia serve` passes the import string
`gaia.api.app:create_app` to uvicorn with `factory=True`, which is what makes `--reload` work.

### A request, layer by layer

```
SecurityHeadersMiddleware   adds nosniff, no-referrer; Cache-Control: no-store on /api/*
 └ RequestContextMiddleware  request id → X-Request-ID header + every log line
    └ TrustedHostMiddleware   Host must be 127.0.0.1 or localhost (DNS-rebinding defence)
       └ router /api/v1       Depends(get_principal): bearer token → Principal, or 401
          └ your route        receives ScopedDep / PrincipalDep / SessionDep
```

### Writing a route

```python
from fastapi import APIRouter, HTTPException
from gaia.api.deps import ScopedDep
from gaia.db.models import Ticket
from gaia.domain.schemas import TicketRead

router = APIRouter(tags=["tickets"])

@router.get("/tickets/{ticket_id}", response_model=TicketRead)
async def get_ticket(ticket_id: str, scoped: ScopedDep) -> TicketRead:
    ticket = await scoped.repo(Ticket).get(ticket_id)
    if ticket is None:
        raise HTTPException(404, "ticket not found")   # same answer for "another workspace"
    return TicketRead.model_validate(ticket)
```

Then include the router in `api_v1_router()`. That puts it under `/api/v1` and behind auth
automatically.

### Auth

`LocalTokenAuth.authenticate()` reads `Authorization: Bearer <token>`, loads the expected token from
the keychain, and compares the two as bytes with `hmac.compare_digest`. On success it returns the
fixed `Principal` (the seeded owner and workspace). On failure it raises `AuthenticationError`,
which becomes a 401 with `WWW-Authenticate: Bearer`. `get_principal` is a plain `def`, so FastAPI
runs the blocking keychain call in its thread pool rather than on the event loop.

The token reaches the browser through `gaia ui`, which prints `http://127.0.0.1:5173/#token=…`.
The part after `#` is never sent to any server.

### Errors

Every failure has the same shape, with the request id also in the `X-Request-ID` header:

```json
{"error": {"code": "not_found", "message": "ticket not found", "request_id": "3f2a..."}}
```

`code` is one of `bad_request`, `unauthorized`, `forbidden`, `not_found`, `method_not_allowed`,
`conflict`, `validation_error`, `internal_error` or `http_error`. Messages are passed through
`redact()`. Validation errors name the field but never echo the submitted value. Crashes are logged
server-side with the traceback, and the client only sees "the server hit an unexpected error".

### The domain routes (doc 06)

| Method | Path | Notes |
|---|---|---|
| POST / GET | `/repositories` | Register (validated by git; origin and default branch detected, credentials stripped) / list |
| GET / DELETE | `/repositories/{id}` | Detail with worktree and active-run counts / unregister, **409 if it has tickets or live worktrees** |
| POST / GET | `/tickets` | Create (repo loaded in your workspace) / page with `status`, `repo_id`, `q`, `limit` ≤ 100, `cursor` → `{items, next_cursor}` |
| GET / PATCH | `/tickets/{id}` | Read / update text fields and status (illegal move → 409) |
| GET | `/agents` | The seeded roster (`lead`, `junior-1`) |
| GET | `/worktrees?repo_id=` | Rows with `orphan`, `exists`, `dirty`, `tampered`, plus `unknown_paths` |
| DELETE | `/worktrees/{id}?force=` | Remove from disk, keep the row; dirty → 409 unless `force`; tampered → always 409 |

Service errors map to status codes in `api/errors.py`: `IllegalTransition`, `WorktreeDirty`,
`WorktreeTampered`, `WorktreeError` and `IntegrityError` → 409; `NotARepository`, `UnsafePath` and
`InvalidRef` → 422. Every mutating route calls `append_audit_log(...)`. `make openapi` regenerates
`frontend/src/api/openapi.json`.

### The event routes (doc 13)

| Method | Path | Auth | Notes |
|---|---|---|---|
| POST | `/api/v1/stream/ticket` | bearer | `{ticket, expires_in}`: single use, 60 s |
| GET | `/api/v1/stream?ticket=&run_id=&last_event_id=` | **ticket only** | `text/event-stream`, live from now, or resumed after `Last-Event-ID` / `last_event_id` |
| GET | `/api/v1/runs/{id}/events?after=&limit=` | bearer | `{items, next_after}`, oldest first, `limit` ≤ 500 |
| POST | `/api/v1/dev/fake-run` | bearer | only with `GAIA_DEV_ROUTES=true`; publishes a scripted run |

`GET /api/v1/stream` is the one `/api/v1` path mounted outside `api_v1_router()`, because a
browser's `EventSource` cannot send a bearer header. The details are in [events](events.md).

## Why it's built this way

- **No `/docs` or `/openapi.json`.** They would be unauthenticated pages. The schema is still
  produced offline with `create_app().openapi()`, which doc 06 exports for the frontend.
- **No CORS middleware.** The UI is served from the same origin as the API (doc 16), so there is
  nothing to allow.
- **Auth on the router, not on each route.** A route someone forgets to protect is still protected.
- **`Scoped`, not a session plus a workspace id.** A route can't forget the workspace filter, because
  it never holds the pieces separately.
- **Inbound request ids are validated** against `[A-Za-z0-9._-]{1,64}`. The header is attacker input,
  and it ends up in logs.

## Changing it safely

- **Commit timing.** `SessionDep` and `ScopedDep` use `Depends(get_session, scope="function")`, so the
  commit runs **before** the response is sent. A failed commit becomes an error response, and a unique
  violation (`IntegrityError`) becomes `409 conflict` with no SQL in the body (05b, review F6).
- **Mutating routes** must write an `audit_log` row (doc 06 onward).
- **Tests** use `httpx.AsyncClient` over `ASGITransport` and enter the lifespan explicitly; use the
  `api` fixture in `tests/conftest.py` (see `tests/test_api_app.py` for examples). A test asserting
  on a 500 body builds its transport with `httpx.ASGITransport(app=app, raise_app_exceptions=False)`.
