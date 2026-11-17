"""Per-model-family auto mode for in-turn command approvals.

With auto mode on for a family, a command the policy would *ask* about runs
without waiting for a human. Commands the policy denies are still denied, every
command still runs in the OS sandbox, and writes to protected files still ask.

Like the pause switch (doc 18a), the state is a small owner-only file in the
data directory: Gaia is single-user and local, the file survives restarts
without a migration, and the data directory is denied to every sandboxed
command, so an agent cannot switch auto mode on for itself.

The file is read on every check, so flipping a switch takes effect on the
agent's next command without a restart. A missing, unreadable or malformed file
means auto mode is **off** for every family.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

import structlog

from gaia.db.types import utcnow

logger = structlog.get_logger(__name__)

#: The state file's name inside the data directory.
AUTO_MODE_FILE = "auto-mode.json"

#: File permissions for the state file: owner read/write only.
FILE_MODE = 0o600


class ModelFamily(StrEnum):
    """The model families an owner can switch auto mode on for, one by one."""

    CLAUDE = "claude"
    DEEPSEEK = "deepseek"


@dataclass(frozen=True)
class AutoModeState:
    """Which families run commands without asking. Everything defaults to off."""

    claude: bool = False
    deepseek: bool = False

    def enabled_for(self, family: ModelFamily) -> bool:
        """Return whether auto mode is on for ``family``."""
        return self.claude if family is ModelFamily.CLAUDE else self.deepseek


class AutoModeSwitch:
    """Read and write the auto-mode state file at ``path``."""

    def __init__(self, path: Path) -> None:
        self._path = path

    def read(self) -> AutoModeState:
        """Return the stored state; anything but a well-formed file means all off."""
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return AutoModeState()
        except (OSError, ValueError):
            logger.warning("auto_mode.unreadable", path=str(self._path))
            return AutoModeState()
        if not isinstance(data, dict):
            logger.warning("auto_mode.unreadable", path=str(self._path))
            return AutoModeState()
        # Only a literal ``true`` switches a family on: a string or a number in
        # a hand-edited file must not.
        return AutoModeState(
            claude=data.get(ModelFamily.CLAUDE.value) is True,
            deepseek=data.get(ModelFamily.DEEPSEEK.value) is True,
        )

    def enabled(self, family: ModelFamily) -> bool:
        """Return whether auto mode is on for ``family`` right now."""
        return self.read().enabled_for(family)

    def write(self, state: AutoModeState) -> None:
        """Store ``state`` atomically (temp file + ``os.replace``), mode ``0o600``."""
        self._path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        payload = {
            ModelFamily.CLAUDE.value: state.claude,
            ModelFamily.DEEPSEEK.value: state.deepseek,
            "updated_at": utcnow().isoformat(),
        }
        fd, temp_name = tempfile.mkstemp(dir=self._path.parent, prefix=".auto-mode-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as state_file:
                json.dump(payload, state_file, sort_keys=True)
                state_file.flush()
                os.fsync(state_file.fileno())
            os.chmod(temp_name, FILE_MODE)
            os.replace(temp_name, self._path)
        finally:
            if os.path.exists(temp_name):
                os.unlink(temp_name)
