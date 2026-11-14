"""Cleanup of runtime subprocesses left behind by a crash (requirement 9).

A runtime records its subprocess group id before the process starts and removes
it after the process exits. If Gaia dies between those two points, the entry
stays in the JSON file and the next startup reaps it - but only after verifying
that the pid still belongs to us. Pids are reused by the OS, so an entry whose
start time or command no longer matches is dropped without a signal.
"""

from __future__ import annotations

import json
import os
import signal
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

from gaia.services.proc import ProcError, run_proc

#: A generous tolerance for comparing process start times: ``ps`` reports whole
#: seconds and the entry is recorded from a clock just before ``exec``.
_START_TIME_TOLERANCE_S = 5.0


@dataclass(frozen=True)
class OrphanEntry:
    """One recorded runtime process group."""

    pid: int
    task_id: str
    started_at: float
    argv0: str


async def is_ours(entry: OrphanEntry) -> bool:
    """Return True when ``entry``'s pid still names the process we recorded.

    Verification is deliberately external to Gaia's own process: it asks ``ps``
    for the pid's start time and command and compares both. A reused pid, or a
    pid now running someone else's binary, does not match.
    """
    try:
        result = await run_proc(
            ["ps", "-o", "lstart=,comm=", "-p", str(entry.pid)],
            cwd=Path("/"),
            timeout_s=5.0,
            env={"LANG": "C"},
        )
    except ProcError:
        # ``ps`` could not be started (a missing binary, or an OS sandbox that
        # denies process listing). Fail closed: never kill what we cannot verify.
        return False
    if not result.ok:
        return False
    output = result.stdout.strip()
    if not output:
        return False
    start_text, separator, command = output.rpartition(" ")
    if not separator:
        return False
    parsed_start = _parse_lstart(start_text)
    if parsed_start is None:
        return False
    start_delta = abs(parsed_start.timestamp() - entry.started_at)
    expected_commands = {entry.argv0.casefold(), Path(entry.argv0).name.casefold()}
    return command.casefold() in expected_commands and start_delta <= _START_TIME_TOLERANCE_S


def _parse_lstart(value: str) -> datetime | None:
    """Parse the ``ps lstart`` column in ``C`` locale."""
    try:
        return datetime.strptime(value, "%a %b %d %H:%M:%S %Y")
    except ValueError:
        return None


def _pid_exists(pid: int) -> bool:
    """Return True when ``pid`` exists, without sending it a signal."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # Exists, but belongs to another user. Do not kill it.
        return True
    return True


class OrphanRegistry:
    """A JSON file of runtime pids, owner-readable only."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def add(self, *, pid: int, task_id: str, started_at: float, argv0: str) -> None:
        """Record one live runtime process group."""
        entries = self._read()
        entries.append(OrphanEntry(pid=pid, task_id=task_id, started_at=started_at, argv0=argv0))
        self._write(entries)

    def remove(self, pid: int) -> None:
        """Drop the entry for ``pid``. Removing an unknown pid is a no-op."""
        entries = self._read()
        remaining = [entry for entry in entries if entry.pid != pid]
        if len(remaining) != len(entries):
            self._write(remaining)

    async def reap_stale(self, *, is_ours: Callable[[OrphanEntry], Awaitable[bool]]) -> None:
        """Kill every stale entry that still belongs to us, then drop all entries.

        Entries are stale by definition once this method runs (startup cleanup):
        anything still running and verified as ours gets a group SIGKILL, and
        every entry is then removed from the file.
        """
        for entry in self._read():
            if _pid_exists(entry.pid) and await is_ours(entry):
                try:
                    os.killpg(entry.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
        self._write([])

    def _read(self) -> list[OrphanEntry]:
        """Read the registry, treating a missing or corrupt file as empty."""
        try:
            raw = self.path.read_text()
        except FileNotFoundError:
            return []
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return []
        if not isinstance(data, list):
            return []
        entries: list[OrphanEntry] = []
        for item in data:
            if not isinstance(item, dict):
                continue
            try:
                entries.append(
                    OrphanEntry(
                        pid=int(item["pid"]),
                        task_id=str(item["task_id"]),
                        started_at=float(item["started_at"]),
                        argv0=str(item["argv0"]),
                    )
                )
            except (KeyError, TypeError, ValueError):
                continue
        return entries

    def _write(self, entries: list[OrphanEntry]) -> None:
        """Write ``entries`` atomically enough for a local JSON file, mode 0600."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps([asdict(entry) for entry in entries], sort_keys=True)
        temporary = self.path.with_name(f"{self.path.name}.tmp")
        temporary.write_text(payload)
        temporary.chmod(0o600)
        temporary.replace(self.path)
