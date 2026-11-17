"""Auto mode: the per-family switch file and its admin routes."""

from __future__ import annotations

import json
import os
import stat
from dataclasses import replace
from pathlib import Path

import pytest

from gaia.db.models import AuditLog
from gaia.db.repo import AuditLogRepo
from gaia.orchestrator.auto_mode import (
    AUTO_MODE_FILE,
    AutoModeState,
    AutoModeSwitch,
    ModelFamily,
)
from tests.conftest import Api, Database

pytestmark = pytest.mark.anyio


def test_a_missing_file_means_auto_mode_is_off(tmp_path: Path) -> None:
    switch = AutoModeSwitch(tmp_path / AUTO_MODE_FILE)

    assert switch.read() == AutoModeState(claude=False, deepseek=False)
    assert switch.enabled(ModelFamily.CLAUDE) is False
    assert switch.enabled(ModelFamily.DEEPSEEK) is False


def test_write_round_trips_per_family_with_owner_only_mode(tmp_path: Path) -> None:
    path = tmp_path / "data" / AUTO_MODE_FILE
    switch = AutoModeSwitch(path)

    switch.write(AutoModeState(claude=False, deepseek=True))

    assert switch.read() == AutoModeState(claude=False, deepseek=True)
    assert switch.enabled(ModelFamily.DEEPSEEK) is True
    assert switch.enabled(ModelFamily.CLAUDE) is False
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert json.loads(path.read_text(encoding="utf-8"))["updated_at"]
    assert [p.name for p in path.parent.iterdir()] == [AUTO_MODE_FILE]


@pytest.mark.parametrize(
    "content",
    [
        "{not json",
        '["claude"]',
        # Only a literal JSON true switches a family on.
        '{"claude": "true", "deepseek": 1}',
    ],
)
def test_a_malformed_file_means_auto_mode_is_off(tmp_path: Path, content: str) -> None:
    path = tmp_path / AUTO_MODE_FILE
    path.write_text(content, encoding="utf-8")

    assert AutoModeSwitch(path).read() == AutoModeState()


def test_an_unreadable_file_means_auto_mode_is_off(tmp_path: Path) -> None:
    path = tmp_path / AUTO_MODE_FILE
    path.mkdir()  # reading a directory raises an OSError

    assert AutoModeSwitch(path).read() == AutoModeState()


def test_a_failed_write_leaves_the_old_state_and_no_temp_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / AUTO_MODE_FILE
    switch = AutoModeSwitch(path)
    switch.write(AutoModeState(claude=True))

    def _fail_replace(src: str, dst: Path) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", _fail_replace)
    with pytest.raises(OSError, match="disk full"):
        switch.write(AutoModeState(deepseek=True))

    assert switch.read() == AutoModeState(claude=True)
    assert [p.name for p in tmp_path.iterdir()] == [AUTO_MODE_FILE]


async def test_auto_mode_routes_answer_503_without_an_orchestrator(api: Api) -> None:
    get = await api.client.get("/api/v1/admin/auto-mode", headers=api.auth)
    patch = await api.client.patch(
        "/api/v1/admin/auto-mode", json={"claude": True}, headers=api.auth
    )

    assert get.status_code == 503
    assert patch.status_code == 503
    assert patch.json()["error"]["code"] == "orchestrator_unavailable"


@pytest.fixture
def auto_api(api: Api) -> Api:
    original = api.app.state.services
    switch = AutoModeSwitch(api.settings.data_dir / AUTO_MODE_FILE)
    api.app.state.services = replace(original, auto_mode=switch)
    return api


async def test_get_auto_mode_defaults_to_off(auto_api: Api) -> None:
    response = await auto_api.client.get("/api/v1/admin/auto-mode", headers=auto_api.auth)

    assert response.status_code == 200
    assert response.json() == {"claude": False, "deepseek": False}


async def test_patch_auto_mode_switches_one_family_and_audits(
    auto_api: Api, database: Database
) -> None:
    on = await auto_api.client.patch(
        "/api/v1/admin/auto-mode", json={"deepseek": True}, headers=auto_api.auth
    )
    also = await auto_api.client.patch(
        "/api/v1/admin/auto-mode", json={"claude": True}, headers=auto_api.auth
    )
    off = await auto_api.client.patch(
        "/api/v1/admin/auto-mode", json={"deepseek": False}, headers=auto_api.auth
    )

    assert on.json() == {"claude": False, "deepseek": True}
    assert also.json() == {"claude": True, "deepseek": True}
    assert off.json() == {"claude": True, "deepseek": False}
    switch: AutoModeSwitch = auto_api.app.state.services.auto_mode
    assert switch.read() == AutoModeState(claude=True, deepseek=False)

    workspace_id: str = auto_api.app.state.services.principal.workspace_id
    async with database.sessions() as session:
        rows = await AuditLogRepo(session, AuditLog, workspace_id).list(action="admin.auto_mode")
    assert [row.details_json["after"] for row in rows] == [
        {"claude": False, "deepseek": True},
        {"claude": True, "deepseek": True},
        {"claude": True, "deepseek": False},
    ]
    assert rows[0].details_json["before"] == {"claude": False, "deepseek": False}


async def test_patch_auto_mode_rejects_unknown_fields(auto_api: Api) -> None:
    response = await auto_api.client.patch(
        "/api/v1/admin/auto-mode", json={"gpt": True}, headers=auto_api.auth
    )

    assert response.status_code == 422


async def test_auto_mode_routes_require_the_token(auto_api: Api) -> None:
    response = await auto_api.client.patch("/api/v1/admin/auto-mode", json={"claude": True})

    assert response.status_code == 401
