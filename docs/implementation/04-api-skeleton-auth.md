# 04 — API Skeleton & Local Auth

## Goal
A FastAPI app that is safe to run on a dev machine: localhost only, token auth, strict
CORS and Host checks, and consistent errors.

## Out of scope
Domain routes (06+). OIDC/multi-user auth (leave the `AuthProvider` interface ready for it).

## Files
- `gaia/api/app.py` (factory `create_app(settings)`)
- `gaia/api/auth.py`
- `gaia/api/errors.py`
- `gaia/api/deps.py` (DB session, current principal, scoped repos)
- `gaia/api/routes/health.py`

## Tasks
1. App factory with a lifespan that opens the DB and loads secrets.
2. `AuthProvider` protocol: `authenticate(request) -> Principal(user_id, workspace_id)`.
   `LocalTokenAuth`:
   - On first run, generate `GAIA_API_TOKEN` with `secrets.token_urlsafe(32)` and store it in the keychain.
   - Accept `Authorization: Bearer <token>`. Compare with `hmac.compare_digest`.
   - `gaia ui` CLI command: print or open `http://127.0.0.1:5173/#token=<token>`. The UI moves the
     token into `sessionStorage` and strips it from the URL. (SSE uses a `?token=` query param
     because `EventSource` cannot set headers. Doc 13 requires a short-lived SSE ticket instead.)
3. Middleware:
   - `TrustedHostMiddleware(allowed_hosts=["127.0.0.1", "localhost"])` to block DNS rebinding.
   - `CORSMiddleware` allowing only `settings.ui_origin`, with explicit methods and headers.
   - Security headers: `X-Content-Type-Options`, `Referrer-Policy: no-referrer`, `Cache-Control: no-store` on API responses.
   - Request-ID middleware that binds the ID into the structlog context.
4. Error envelope: `{"error": {"code": str, "message": str, "request_id": str}}`. Never return stack traces.
5. `GET /api/health` (no auth) → `{"status":"ok"}`. Every other route goes under `/api/v1` and requires auth.
6. `make dev-backend` runs uvicorn with host and port taken from settings.

## Tests
- No token gives 401. A wrong token gives 401. A valid token gives 200.
- `Host: evil.com` gives 400.
- A CORS preflight from another origin is refused.
- An error response contains no traceback.

## Acceptance criteria
- [ ] `curl 127.0.0.1:<port>/api/v1/...` without a token fails. With the token from the keychain it succeeds.
- [ ] The server refuses to start on `0.0.0.0` unless explicitly overridden.
