# 04 — API Skeleton & Local Auth

**Status:** done (PR `impl/04-api-skeleton-auth`). The tasks below are the plan as written;
"Decisions and deviations as built" records what actually shipped.

## Goal
A FastAPI app that is safe to run on a dev machine: localhost only, token auth, strict
CORS and Host checks, and consistent errors.

## Out of scope
Domain routes (06+). OIDC/multi-user auth (leave the `AuthProvider` interface ready for it).
SSE stream tickets (13): they are short-lived and unrelated to this long-lived token.

## Files
- `gaia/api/app.py`: `create_app(settings)`, the lifespan, and the two middlewares (task 3).
- `gaia/api/auth.py`: `Principal`, `AuthProvider`, `LocalTokenAuth`, `bearer_token()`.
- `gaia/api/errors.py`: `error_response()`, the handlers, `install_error_handlers()`, and the
  request-id format the envelope carries.
- `gaia/api/deps.py`: `AppServices`, `get_session`, `get_principal`, `Scoped`, `get_scoped`.
- `gaia/api/routes/health.py`: `GET /api/health`.
- `gaia/api/routes/session.py`: `GET /api/v1/me` (added; see deviation 5).
- `gaia/config/secrets.py`: `ensure_api_token()` (task 2's generation, next to the store).
- `gaia/cli.py`: `gaia serve`, `gaia ui`, and a one-line error for bad settings.
- `Makefile`: `dev-backend` (task 6). `README.md`: how to start the server and log in.

## Endpoints (as built)

| Method | Path | Auth | Body |
|---|---|---|---|
| GET | `/api/health` | none | `{"status": "ok"}` |
| GET | `/api/v1/me` | `Authorization: Bearer <token>` | `{"user_id", "workspace_id"}` |

Every error, including a crash, is `{"error": {"code", "message", "request_id"}}` with an
`X-Request-ID` header carrying the same id. `code` is one of `bad_request`, `unauthorized`,
`forbidden`, `not_found`, `method_not_allowed`, `conflict`, `validation_error`,
`internal_error`, `http_error`.

Routes are registered on a router that carries the auth dependency, so a route added later
(docs 06, 13, 14) is authenticated by default rather than by its author's memory.

## Tasks (as built)
1. `create_app(settings)` with a lifespan that: configures logging, creates `~/.gaia` at `0o700`,
   opens the engine, seeds the workspace and owner (doc 03), generates the API token, and disposes
   the engine on shutdown. An unmigrated database fails at startup with ``run `make migrate` ``.
2. `AuthProvider.authenticate(request) -> Principal`; `LocalTokenAuth` compares the bearer token
   with `hmac.compare_digest` on bytes (comparing `str` raises on non-ASCII input, which would turn
   a 401 into a 500).
3. `TrustedHostMiddleware(["127.0.0.1", "localhost"])`, no CORS middleware, security headers, and a
   request-id middleware that binds the id into the structlog context.
4. The error envelope, with a redacted message and no traceback, exception text or submitted input.
5. `GET /api/health` is the only route without auth; everything else lives under `/api/v1`.
6. `make dev-backend` runs `gaia serve --reload`, which passes `Settings.host` and `Settings.port`
   to uvicorn.

The original task text, for reference: generation happens on first run with
`secrets.token_urlsafe(32)`; `gaia ui` prints `http://127.0.0.1:5173/#token=<token>`; the UI moves
the token into `sessionStorage` and strips it from the URL (`EventSource` cannot set headers, so SSE
uses doc 13's short-lived ticket, never this token in a URL); if CORS is ever added it allows only
`settings.ui_origin`.

## Tests
`tests/test_api_app.py`, `tests/test_api_auth.py`, `tests/test_api_deps.py`, plus additions to
`tests/test_cli.py` and `tests/test_secrets.py` (55 new tests):

- no token → 401 with `WWW-Authenticate: Bearer`; a wrong token → 401; the token that resolves to
  the seeded owner → 200 with that workspace's id (not a constant);
- `Host: evil.com` → 400; a CORS preflight from another origin is refused and carries no
  `Access-Control-Allow-Origin`;
- a crash → 500 with no traceback, no exception class and no secret in the body (the test raises
  with a `sk-ant-…` value in the message and asserts it never comes back);
- a validation error names the field, never the submitted value;
- request ids: generated when absent, kept when plausible, replaced when not;
- startup: seeds one workspace and one owner, generates a 43-character token once, reuses it on a
  second start, and names `make migrate` on an empty database;
- `Scoped` cannot read another workspace's row, and a route that writes through it commits into the
  authenticated principal's workspace.

## Acceptance criteria
- [x] `curl 127.0.0.1:<port>/api/v1/...` without a token fails. With the token from the keychain it
      succeeds. Evidence: with `gaia serve` running on 8765 against the real macOS Keychain,
      `GET /api/v1/me` returned 401 (`a bearer token is required`), 401 with a wrong token, and 200
      with the keychain token; `GET /api/health` returned 200 without one. The token (43 characters,
      generated on that first run) appears nowhere in the server's log.
- [x] The server refuses to start on `0.0.0.0` unless explicitly overridden. Evidence: `Settings`'
      loopback validator (doc 02) runs before uvicorn sees the address, so `GAIA_HOST=0.0.0.0 gaia
      serve` exits 2 with `host '0.0.0.0' is not a loopback address; set allow_remote=True to bind
      to another address`, while `GAIA_ALLOW_REMOTE=true` binds it (`tests/test_cli.py`).
- [x] `make check` passes: 242 tests, 100% backend coverage, mypy strict clean, gitleaks clean.

## Decisions and deviations (as built)
1. **The token is generated in `gaia/config/secrets.py` (`ensure_api_token`), not in
   `LocalTokenAuth`.** Task 2 puts generation on the auth provider, but `gaia ui` needs the same
   "generate if missing" behaviour and must not import FastAPI and uvicorn to get it. One function
   creates, the provider only compares. A missing token at request time answers "no API token is
   configured; restart the server" rather than `require()`'s advice, which would tell the owner to
   run `gaia secrets set GAIA_API_TOKEN` - a command the CLI refuses on purpose.
2. **`gaia ui` prints the URL; it does not open a browser.** Only `gaia/services/proc.py` may spawn a
   process and it arrives with doc 05, so `TODO(gaia-05)` marks the `--open` flag. The token travels
   in the fragment, which a browser never sends to a server, and never through `argv`.
3. **`gaia serve` is a new CLI subcommand, and `make dev-backend` calls it.** uvicorn cannot read
   `Settings`, so something has to bridge them. It passes the import string
   `gaia.api.app:create_app` with `factory=True`, which is what makes `--reload` work: the reloader
   re-imports the target instead of receiving a pickled app.
4. **No interactive documentation routes.** `docs_url`, `redoc_url` and `openapi_url` are `None`:
   task 5 says `/api/health` is the only route that answers without auth, and FastAPI's docs pages
   cannot sit behind a bearer token. `create_app().openapi()` still produces the schema doc 06
   exports, and it declares the `bearer` security scheme.
5. **`GET /api/v1/me` was added.** The doc's tests need one authenticated route to prove 401/200, and
   the UI needs a way to turn a 401 into the "run `gaia ui`" screen (doc 16). It lives in a new
   `gaia/api/routes/session.py` rather than in `health.py`, because `/api/health` is deliberately the
   unauthenticated one.
6. **A rejected `Host` is Starlette's plain-text 400, not the envelope.** `TrustedHostMiddleware`
   writes that response itself; replacing it means reimplementing its check, and two host checks
   that could disagree are worse than one response that is not JSON. It still carries the security
   headers and `X-Request-ID` (see 7), so the rejection is traceable.
7. **The middleware stack is inverted from the order of the `add_middleware` calls.** Starlette
   inserts at the front, so the calls run `TrustedHost` → `RequestContext` → `SecurityHeaders` and
   the stack is `SecurityHeaders` (outermost) → `RequestContext` → `TrustedHost`. The headers
   therefore cover the 400 above, and the request id is bound before the host is checked.
8. **An inbound `X-Request-ID` is trusted only when it matches `[A-Za-z0-9._-]{1,64}`.** It is echoed
   and logged, so an unvalidated value would let a caller write arbitrary text into a log line.
9. **`deps.get_session` commits after the response is sent.** That has been FastAPI's behaviour
   since 0.106 (the dependency's exit stack unwinds after `response(...)`), so the commit cannot
   change a status code that has already left. Doc 06's mutating routes must commit inside the
   handler when a failed commit has to be reported to the client.
10. **The dependency is a `Scoped` object, not `(session, workspace_id)`.** A route cannot forget the
    workspace filter because there is nothing to forget (doc 03 task 5, ARCHITECTURE section 7).
11. **Tests use `httpx.AsyncClient` over `ASGITransport` and enter the lifespan themselves.**
    `starlette.testclient` imports a deprecated `httpx` shim, which `filterwarnings = ["error"]`
    turns into a failure, and installing `httpx2` would be a dependency this doc does not name.
    Doc 06 tests the API the same way.
12. **A `Settings` validation error exits 2 with one line** instead of a traceback: setting
    `GAIA_HOST` is the accepted way to get a bad bind address, and the owner needs to read the fix.
13. **No new dependency.** `uvicorn[standard]` and `httpx` were already pinned by doc 01.
