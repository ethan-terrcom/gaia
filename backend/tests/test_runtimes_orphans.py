"""Tests for the orphan registry (doc 10a requirement 9)."""

from __future__ import annotations

import json
import os
import stat
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path

import pytest

from gaia.runtimes.orphans import (
    OrphanEntry,
    OrphanRegistry,
    _parse_lstart,
    _pid_exists,
    is_ours,
)
from gaia.services.proc import ProcError, ProcResult


def test_add_and_remove_persist_0600(tmp_path: Path) -> None:
    path = tmp_path / "runtime-pids.json"
    registry = OrphanRegistry(path)

    registry.add(pid=10, task_id="task-1", started_at=1.5, argv0="python")
    registry.add(pid=11, task_id="task-2", started_at=2.5, argv0="python")
    assert stat.S_IMODE(path.stat().st_mode) == 0o600

    registry.remove(10)

    data = json.loads(path.read_text())
    assert data == [{"pid": 11, "task_id": "task-2", "started_at": 2.5, "argv0": "python"}]


def test_read_treats_missing_corrupt_and_malformed_as_empty(tmp_path: Path) -> None:
    registry = OrphanRegistry(tmp_path / "runtime-pids.json")

    assert registry._read() == []

    registry.path.write_text("{not-json")
    assert registry._read() == []

    registry.path.write_text('{"not": "a list"}')
    assert registry._read() == []

    registry.path.write_text('[{"pid": "not-an-int"}]')
    assert registry._read() == []

    registry.path.write_text('["not-a-dict"]')
    assert registry._read() == []


async def test_reap_stale_drops_every_entry(tmp_path: Path) -> None:
    registry = OrphanRegistry(tmp_path / "runtime-pids.json")
    registry.add(
        pid=os.getpid(),
        task_id="task-1",
        started_at=1.5,
        argv0="python",
    )
    called: list[OrphanEntry] = []

    async def fake_is_ours(entry: OrphanEntry) -> bool:
        called.append(entry)
        return False

    await registry.reap_stale(is_ours=fake_is_ours)

    assert called == [
        OrphanEntry(pid=os.getpid(), task_id="task-1", started_at=1.5, argv0="python")
    ]
    assert registry._read() == []
    assert json.loads(registry.path.read_text()) == []


async def test_is_ours_returns_false_when_ps_has_no_such_pid() -> None:
    assert await is_ours(OrphanEntry(pid=2**22 + 1, task_id="t", started_at=0, argv0="x")) is False


async def _with_ps_output(
    monkeypatch: pytest.MonkeyPatch,
    *,
    returncode: int = 0,
    stdout: str = "",
) -> None:
    async def fake_run_proc(
        argv: Sequence[str], cwd: Path, timeout_s: float, env: dict[str, str] | None
    ) -> ProcResult:
        del argv, cwd, timeout_s, env
        return ProcResult(
            argv=(),
            cwd=Path("/"),
            pid=0,
            returncode=returncode,
            stdout=stdout,
            stderr="",
            stdout_truncated=False,
            stderr_truncated=False,
            duration_s=0,
            timed_out=False,
        )

    monkeypatch.setattr("gaia.runtimes.orphans.run_proc", fake_run_proc)


async def test_is_ours_false_when_ps_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _with_ps_output(monkeypatch, returncode=1)

    assert await is_ours(OrphanEntry(pid=1, task_id="t", started_at=0, argv0="x")) is False


async def test_is_ours_false_when_ps_output_is_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _with_ps_output(monkeypatch, stdout="")

    assert await is_ours(OrphanEntry(pid=1, task_id="t", started_at=0, argv0="x")) is False


async def test_is_ours_false_when_ps_output_has_no_separator(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _with_ps_output(monkeypatch, stdout="no-separator")

    assert await is_ours(OrphanEntry(pid=1, task_id="t", started_at=0, argv0="x")) is False


async def test_is_ours_false_when_start_time_is_garbage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _with_ps_output(monkeypatch, stdout="not-a-date python")

    assert await is_ours(OrphanEntry(pid=1, task_id="t", started_at=0, argv0="x")) is False


def test_pid_exists_checks_process_and_permission(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert _pid_exists(2**22 + 1) is False

    def _raise_permission(pid: int, sig: int) -> None:
        del pid, sig
        raise PermissionError

    monkeypatch.setattr("gaia.runtimes.orphans.os.kill", _raise_permission)
    assert _pid_exists(1) is True


async def test_reap_stale_ignores_a_process_that_vanished_before_kill(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    registry = OrphanRegistry(tmp_path / "runtime-pids.json")
    registry.add(pid=1, task_id="t", started_at=0, argv0="x")

    async def fake_is_ours(entry: OrphanEntry) -> bool:
        del entry
        return True

    def _raise_process_lookup(pid: int, sig: int) -> None:
        del pid, sig
        raise ProcessLookupError

    monkeypatch.setattr("gaia.runtimes.orphans._pid_exists", lambda pid: True)
    monkeypatch.setattr("gaia.runtimes.orphans.os.killpg", _raise_process_lookup)

    await registry.reap_stale(is_ours=fake_is_ours)
    start_text = "Thu Nov 14 10:00:00 2026"
    started_at = datetime.strptime(start_text, "%a %b %d %H:%M:%S %Y").timestamp()

    async def fake_run_proc(
        argv: Sequence[str], cwd: Path, timeout_s: float, env: dict[str, str] | None
    ) -> ProcResult:
        del timeout_s, env
        return ProcResult(
            argv=tuple(argv),
            cwd=Path(cwd),
            pid=int(argv[-1]),
            returncode=0,
            stdout=f"{start_text} Python\n",
            stderr="",
            stdout_truncated=False,
            stderr_truncated=False,
            duration_s=0,
            timed_out=False,
        )

    monkeypatch.setattr("gaia.runtimes.orphans.run_proc", fake_run_proc)

    assert (
        await is_ours(OrphanEntry(pid=123, task_id="t", started_at=started_at, argv0="python"))
        is True
    )


def test_parse_lstart_rejects_garbage() -> None:
    assert _parse_lstart("not a date") is None


async def test_is_ours_fails_closed_when_ps_cannot_start(monkeypatch: pytest.MonkeyPatch) -> None:
    """Lead review 10a: deterministic, not dependent on whether ps runs in this environment."""

    async def no_ps(*args: object, **kwargs: object) -> object:
        raise ProcError("could not run 'ps'")

    monkeypatch.setattr("gaia.runtimes.orphans.run_proc", no_ps)

    assert (
        await is_ours(OrphanEntry(pid=os.getpid(), task_id="t", started_at=0, argv0="x")) is False
    )
