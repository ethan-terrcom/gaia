"""Live sandbox tests for the command runner (run by the lead, not in CI).

These tests need a real ``srt`` binary because macOS refuses to nest
``sandbox-exec`` inside the harness sandbox. Set ``GAIA_SRT_PATH`` to the
pinned executable to run them; otherwise every test is skipped.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from gaia.config.settings import Settings
from gaia.tools.shell import SrtCommandRunner

_GAIA_SRT_PATH = os.environ.get("GAIA_SRT_PATH")
pytestmark = pytest.mark.skipif(
    not _GAIA_SRT_PATH or not Path(_GAIA_SRT_PATH).is_file(),
    reason="GAIA_SRT_PATH must point at the pinned sandbox-runtime executable",
)

PYTHON = sys.executable


@pytest.fixture
def srt_path() -> Path:
    assert _GAIA_SRT_PATH is not None
    return Path(_GAIA_SRT_PATH)


@pytest.fixture
def worktree(tmp_path: Path) -> Path:
    path = tmp_path / "worktree"
    path.mkdir()
    return path


@pytest.fixture
def task_dir(tmp_path: Path) -> Path:
    path = tmp_path / "task-tmp"
    path.mkdir()
    return path


async def test_command_runner_blocks_secret_read_and_network(
    srt_path: Path,
    tmp_path: Path,
    worktree: Path,
    task_dir: Path,
) -> None:
    data_dir = tmp_path / "gaia-data"
    data_dir.mkdir()
    (data_dir / "gaia.db").write_text("not for agents", encoding="utf-8")
    settings = Settings(data_dir=data_dir)
    code = (
        "from pathlib import Path\n"
        "import socket\n"
        "READ_DENIED = False\n"
        "NETWORK_DENIED = False\n"
        "try:\n"
        f"    Path({str(data_dir / 'gaia.db')!r}).read_text()\n"
        "except OSError:\n"
        "    READ_DENIED = True\n"
        "try:\n"
        "    with socket.create_connection(('1.1.1.1', 443), timeout=2):\n"
        "        pass\n"
        "except OSError:\n"
        "    NETWORK_DENIED = True\n"
        "assert READ_DENIED\n"
        "assert NETWORK_DENIED\n"
    )
    runner = SrtCommandRunner(settings, srt_path=srt_path)

    result = await runner.run(
        [PYTHON, "-c", code],
        cwd=worktree,
        timeout_s=60,
        task_dir=task_dir,
    )

    assert result.ok, result.stderr


async def test_command_runner_runs_an_allowed_command(
    srt_path: Path,
    tmp_path: Path,
    worktree: Path,
    task_dir: Path,
) -> None:
    settings = Settings(data_dir=tmp_path / "gaia-data")
    runner = SrtCommandRunner(settings, srt_path=srt_path)

    result = await runner.run(
        [PYTHON, "-m", "pytest", "--version"],
        cwd=worktree,
        timeout_s=60,
        task_dir=task_dir,
    )

    assert result.ok, result.stderr
