"""The owner's default agent mode: a small owner-only file in the data dir (doc 19b).

Like auto mode (and the pause flag before it), the default is a file rather than
a database row: Gaia is single-user and local, the file survives restarts without
a migration, and the data directory is denied to every sandboxed command, so an
agent cannot change which models the next run uses.

A missing or malformed file means "no saved default"; callers then fall back to
the settings-derived default. Runs never read this file after they start: each
run stores its own config.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

import structlog
from pydantic import ValidationError

from gaia.domain.agent_modes import AgentModeConfig

logger = structlog.get_logger(__name__)

#: The state file's name inside the data directory.
AGENT_MODE_FILE = "agent-mode.json"

#: File permissions for the state file: owner read/write only.
FILE_MODE = 0o600


class AgentModeStore:
    """Read and write the default :class:`AgentModeConfig` at ``path``."""

    def __init__(self, path: Path) -> None:
        self._path = path

    def read(self) -> AgentModeConfig | None:
        """Return the saved default, or ``None`` when there is no usable file."""
        try:
            raw = self._path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        except OSError:
            logger.warning("agent_mode.unreadable", path=str(self._path))
            return None
        try:
            return AgentModeConfig.model_validate_json(raw)
        except ValidationError:
            logger.warning("agent_mode.invalid", path=str(self._path))
            return None

    def write(self, config: AgentModeConfig) -> None:
        """Store ``config`` atomically (temp file + ``os.replace``), mode ``0o600``."""
        self._path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(
            dir=self._path.parent, prefix=".agent-mode-", suffix=".tmp"
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as state_file:
                json.dump(config.model_dump(mode="json"), state_file, sort_keys=True)
                state_file.flush()
                os.fsync(state_file.fileno())
            os.chmod(temp_name, FILE_MODE)
            os.replace(temp_name, self._path)
        finally:
            if os.path.exists(temp_name):
                os.unlink(temp_name)
