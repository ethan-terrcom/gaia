"""Built-UI serving: SPA fallback, static assets and the shared CSP (doc 16)."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

from gaia.api.app import create_app
from gaia.api.spa import CSP, SPA_HEADERS, mount_spa
from gaia.config.settings import Settings
from tests.conftest import API_BASE_URL


def _make_dist(tmp_path: Path) -> Path:
    dist = tmp_path / "dist"
    assets = dist / "assets"
    assets.mkdir(parents=True)
    (dist / "index.html").write_text("<!doctype html><title>Gaia</title>", encoding="utf-8")
    (assets / "app.js").write_text("console.log('gaia');", encoding="utf-8")
    return dist


def _make_app(tmp_path: Path, dist: Path | None) -> FastAPI:
    settings = Settings(
        data_dir=tmp_path / "gaia-data",
        db_url=f"sqlite+aiosqlite:///{tmp_path / 'spa.sqlite'}",
        ui_dist_dir=dist,
    )
    return create_app(settings)


async def _client(app: FastAPI) -> httpx.AsyncClient:
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url=API_BASE_URL)


def test_csp_matches_the_frontend_copy() -> None:
    frontend_csp = Path(__file__).resolve().parents[2] / "frontend" / "csp.json"
    value = json.loads(frontend_csp.read_text(encoding="utf-8"))["value"]

    assert value == CSP


async def test_the_spa_fallback_serves_index_with_security_headers(tmp_path: Path) -> None:
    app = _make_app(tmp_path, _make_dist(tmp_path))
    async with await _client(app) as client:
        response = await client.get("/runs/abc")

    assert response.status_code == 200
    assert response.text == "<!doctype html><title>Gaia</title>"
    assert response.headers["content-type"] == "text/html; charset=utf-8"
    assert response.headers["content-security-policy"] == CSP
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["referrer-policy"] == "no-referrer"


async def test_an_api_path_keeps_its_json_404(tmp_path: Path) -> None:
    app = _make_app(tmp_path, _make_dist(tmp_path))
    async with await _client(app) as client:
        response = await client.get("/api/nope")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


async def test_assets_are_served_by_static_files(tmp_path: Path) -> None:
    app = _make_app(tmp_path, _make_dist(tmp_path))
    async with await _client(app) as client:
        response = await client.get("/assets/app.js")

    assert response.status_code == 200
    assert response.text == "console.log('gaia');"


async def test_asset_traversal_does_not_escape_the_dist_directory(tmp_path: Path) -> None:
    dist = _make_dist(tmp_path)
    (dist / "secret.txt").write_text("do-not-serve", encoding="utf-8")
    app = _make_app(tmp_path, dist)
    async with await _client(app) as client:
        response = await client.get("/assets/../../secret.txt")

    assert "do-not-serve" not in response.text


async def test_the_spa_is_not_mounted_when_ui_dist_dir_is_unset(tmp_path: Path) -> None:
    app = _make_app(tmp_path, None)
    async with await _client(app) as client:
        response = await client.get("/runs/abc")

    assert response.status_code == 404


async def test_mount_spa_rejects_a_missing_assets_directory(tmp_path: Path) -> None:
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("spa", encoding="utf-8")
    app = FastAPI()

    with pytest.raises(RuntimeError, match="assets directory"):
        mount_spa(app, dist)


async def test_spa_headers_constant_is_complete() -> None:
    assert SPA_HEADERS["Content-Security-Policy"] == CSP
    assert SPA_HEADERS["Cache-Control"] == "no-store"
