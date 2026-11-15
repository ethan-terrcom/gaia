"""Tests for the durable global kill-switch flag (doc 18a)."""

from __future__ import annotations

import json
import os
import stat
from datetime import UTC, datetime
from pathlib import Path

import pytest

from gaia.orchestrator.pause import OrchestratorPaused, PauseSwitch


def _mode(path: Path) -> int:
    return stat.S_IMODE(os.stat(path).st_mode)


def test_pause_switch_round_trip(tmp_path: Path) -> None:
    switch = PauseSwitch(tmp_path / "paused")

    assert switch.is_paused() is False
    assert switch.info() is None

    switch.pause("going out")

    assert switch.is_paused() is True
    info = switch.info()
    assert info is not None
    assert info.reason == "going out"
    assert info.since.tzinfo is not None

    switch.resume()

    assert switch.is_paused() is False
    assert switch.info() is None


def test_pause_file_is_owner_only(tmp_path: Path) -> None:
    path = tmp_path / "paused"
    PauseSwitch(path).pause("reason")

    assert _mode(path) == 0o600


def test_pause_writes_atomically_and_leaves_no_temp_file(tmp_path: Path) -> None:
    path = tmp_path / "paused"
    PauseSwitch(path).pause("reason")

    assert list(tmp_path.glob(".paused-*")) == []
    data = json.loads(path.read_text(encoding="utf-8"))
    assert set(data) == {"since", "reason"}
    datetime.fromisoformat(data["since"])  # valid ISO, not a partial write


def test_pause_overwrites_the_existing_flag(tmp_path: Path) -> None:
    switch = PauseSwitch(tmp_path / "paused")
    switch.pause("first")
    first = switch.info()
    assert first is not None

    switch.pause("second")
    second = switch.info()
    assert second is not None
    assert second.reason == "second"
    assert second.since != first.since


def test_resume_when_not_paused_is_a_noop(tmp_path: Path) -> None:
    switch = PauseSwitch(tmp_path / "paused")
    switch.resume()
    assert switch.is_paused() is False


def test_info_since_is_utc_aware(tmp_path: Path) -> None:
    switch = PauseSwitch(tmp_path / "paused")
    switch.pause("utc")
    info = switch.info()
    assert info is not None
    assert info.since.utcoffset() is not None
    # The flag is always written as UTC, so an aware round-trip keeps the zone.
    assert info.since.astimezone(UTC).isoformat()


def test_orchestrator_paused_without_a_reason_uses_the_plain_message() -> None:
    with pytest.raises(OrchestratorPaused, match="the orchestrator is paused"):
        raise OrchestratorPaused()


def test_pause_cleans_up_the_temp_file_when_replace_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    switch = PauseSwitch(tmp_path / "paused")

    def fail_replace(source: str, target: str) -> None:
        raise OSError("crash")

    monkeypatch.setattr("gaia.orchestrator.pause.os.replace", fail_replace)
    with pytest.raises(OSError, match="crash"):
        switch.pause("reason")

    assert list(tmp_path.glob(".paused-*")) == []
