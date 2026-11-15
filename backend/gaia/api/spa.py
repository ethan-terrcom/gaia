"""Serve the built frontend from the API process (doc 16 task 7).

The SPA and its assets are same-origin by construction: ``/assets/*`` is a
Starlette ``StaticFiles`` mount, and every other non-``/api`` GET returns the
built ``index.html`` with the production CSP. The catch-all route is added with
``include_in_schema=False`` so the generated OpenAPI document still describes
only JSON routes.
"""

from __future__ import annotations

from pathlib import Path
from typing import Final

from fastapi import FastAPI, HTTPException, Request, status
from fastapi.responses import Response
from starlette.staticfiles import StaticFiles

#: The production content security policy. Keep in sync with ``frontend/csp.json``;
#: a backend test asserts the two copies are equal.
CSP: Final = (
    "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
    "connect-src 'self'; font-src 'self'; object-src 'none'; base-uri 'none'; "
    "form-action 'self'; frame-ancestors 'none'"
)

#: The SPA fallback is never cached: it is the page that bootstraps the app.
SPA_HEADERS: Final = {
    "Content-Security-Policy": CSP,
    "Cache-Control": "no-store",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
}


def mount_spa(app: FastAPI, dist_dir: Path) -> None:
    """Mount ``dist_dir`` and add the SPA fallback route.

    ``create_app`` calls this only after checking that ``dist_dir / index.html``
    exists. The route intentionally never builds a file path from the URL: asset
    paths are resolved exclusively by :class:`StaticFiles`.
    """
    index_path = dist_dir / "index.html"
    index_html = index_path.read_bytes()
    assets_dir = dist_dir / "assets"
    if not assets_dir.is_dir():
        raise RuntimeError(f"the built UI has no assets directory: {assets_dir}")

    app.mount("/assets", StaticFiles(directory=assets_dir), name="assets")

    async def spa_fallback(request: Request) -> Response:
        """Return ``index.html`` for any non-API GET, preserving API 404s."""
        if request.url.path.startswith("/api/"):
            raise HTTPException(status.HTTP_404_NOT_FOUND, "not found")
        return Response(content=index_html, media_type="text/html", headers=dict(SPA_HEADERS))

    app.add_api_route(
        "/{full_path:path}",
        spa_fallback,
        methods=["GET"],
        include_in_schema=False,
        name="spa_fallback",
    )
