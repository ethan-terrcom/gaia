"""The durable global kill switch (doc 18a).

Gaia is single-user and local, so "paused" is a file in the data directory
rather than a database row: it survives restarts without a migration, and there
is no second workspace that would need its own flag.

The file is deliberately tiny and owner-only (``0o600``). It is written
atomically with a temp file + :func:`os.replace` so a crash while flipping the
switch leaves either the old state or the new state, never a half-written JSON
file.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from gaia.db.types import utcnow

#: File permissions for the flag: owner read/write only.
FLAG_MODE = 0o600


class OrchestratorPaused(RuntimeError):
    """A start/resume/gate decision was refused because the kill switch is on."""

    def __init__(self, reason: str | None = None) -> None:
        self.reason = reason
        message = "the orchestrator is paused"
        if reason:
            message = f"{message}: {reason}"
        super().__init__(message)


@dataclass(frozen=True)
class PauseInfo:
    """The contents of the pause flag, read back for the admin endpoint."""

    since: datetime
    reason: str | None = None


class PauseSwitch:
    """Read and flip the process-wide pause flag at ``path``."""

    def __init__(self, path: Path) -> None:
        self._path = path

    def is_paused(self) -> bool:
        """Return whether the flag file currently exists."""
        return self._path.exists()

    def pause(self, reason: str) -> None:
        """Write the flag file atomically, with mode ``0o600``."""
        self._path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        payload = {"since": utcnow().isoformat(), "reason": reason}
        fd, temp_name = tempfile.mkstemp(dir=self._path.parent, prefix=".paused-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as flag_file:
                json.dump(payload, flag_file, sort_keys=True)
                flag_file.flush()
                os.fsync(flag_file.fileno())
            os.chmod(temp_name, FLAG_MODE)
            os.replace(temp_name, self._path)
        finally:
            if os.path.exists(temp_name):
                os.unlink(temp_name)

    def resume(self) -> None:
        """Delete the flag file; resuming an already-running orchestrator is a no-op."""
        self._path.unlink(missing_ok=True)

    def info(self) -> PauseInfo | None:
        """Return the flag's contents, or ``None`` when the orchestrator is not paused."""
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        return PauseInfo(
            since=datetime.fromisoformat(data["since"]),
            reason=data.get("reason"),
        )
