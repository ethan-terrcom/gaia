"""Live sandbox tests (run by the lead, not in normal CI).

These tests need a real ``srt`` binary because macOS refuses to nest
``sandbox-exec`` inside the harness sandbox. Set ``GAIA_SRT_PATH`` to the
pinned executable to run them; otherwise every test is skipped.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from gaia.security.sandbox import SandboxSpec, wrap_argv, write_srt_settings
from gaia.services.proc import run_proc

_GAIA_SRT_PATH = os.environ.get("GAIA_SRT_PATH")
pytestmark = pytest.mark.skipif(
    not _GAIA_SRT_PATH or not Path(_GAIA_SRT_PATH).is_file(),
    reason="GAIA_SRT_PATH must point at the pinned sandbox-runtime executable",
)

PYTHON = sys.executable


@pytest.fixture
def srt_path() -> Path:
    # Read at import time: the autouse settings fixture scrubs GAIA_* variables.
    assert _GAIA_SRT_PATH is not None
    return Path(_GAIA_SRT_PATH)


@pytest.fixture
def fake_home(tmp_path: Path) -> Path:
    home = tmp_path / "home"
    (home / ".ssh").mkdir(parents=True)
    (home / ".ssh" / "id_test").write_text("fake-secret-key", encoding="utf-8")
    return home


@pytest.fixture
def worktree(tmp_path: Path) -> Path:
    path = tmp_path / "worktree"
    path.mkdir()
    return path


@pytest.fixture
def tmp_dir(tmp_path: Path) -> Path:
    path = tmp_path / "task-tmp"
    path.mkdir()
    return path


def _settings_path(
    srt_path: Path, tmp_path: Path, home: Path, worktree: Path, tmp_dir: Path
) -> Path:
    spec = SandboxSpec(worktree=worktree, tmp_dir=tmp_dir, home=home)
    return write_srt_settings(spec, tmp_path / "srt-settings")


async def test_sandbox_blocks_secret_read_and_network(
    srt_path: Path,
    tmp_path: Path,
    fake_home: Path,
    worktree: Path,
    tmp_dir: Path,
) -> None:
    """The doc's test: a conftest.py that reads ~/.ssh and calls the network fails."""
    key_path = fake_home / ".ssh" / "id_test"
    (worktree / "conftest.py").write_text(
        "from pathlib import Path\n"
        "import socket\n"
        f"KEY = Path({str(key_path)!r})\n"
        "READ_DENIED = False\n"
        "NETWORK_DENIED = False\n"
        "try:\n"
        "    KEY.read_text()\n"
        "except OSError:\n"
        "    READ_DENIED = True\n"
        "try:\n"
        "    with socket.create_connection(('1.1.1.1', 443), timeout=2):\n"
        "        pass\n"
        "except OSError:\n"
        "    NETWORK_DENIED = True\n",
        encoding="utf-8",
    )
    (worktree / "test_escape.py").write_text(
        "import conftest\n"
        "def test_both_failed():\n"
        "    assert conftest.READ_DENIED\n"
        "    assert conftest.NETWORK_DENIED\n",
        encoding="utf-8",
    )

    settings_path = _settings_path(srt_path, tmp_path, fake_home, worktree, tmp_dir)
    result = await run_proc(
        wrap_argv(srt_path, settings_path, [PYTHON, "-m", "pytest", "-q", "test_escape.py"]),
        cwd=worktree,
        env={"HOME": str(tmp_dir), "TMPDIR": str(tmp_dir)},
        timeout_s=60,
    )

    assert result.ok, result.stderr


async def test_sandbox_blocks_writes_outside_the_worktree(
    srt_path: Path,
    tmp_path: Path,
    fake_home: Path,
    worktree: Path,
    tmp_dir: Path,
) -> None:
    outside = tmp_path / "outside.txt"
    code = (
        "from pathlib import Path\n"
        f"try:\n"
        f"    Path({str(outside)!r}).write_text('x')\n"
        "    print('WROTE')\n"
        "except OSError:\n"
        "    print('DENIED')\n"
    )
    settings_path = _settings_path(srt_path, tmp_path, fake_home, worktree, tmp_dir)

    result = await run_proc(
        wrap_argv(srt_path, settings_path, [PYTHON, "-c", code]),
        cwd=worktree,
        env={"HOME": str(tmp_dir), "TMPDIR": str(tmp_dir)},
        timeout_s=60,
    )

    assert result.ok, result.stderr
    assert "DENIED" in result.stdout


async def test_sandbox_blocks_writes_to_the_worktree_git_dir(
    srt_path: Path,
    tmp_path: Path,
    fake_home: Path,
    worktree: Path,
    tmp_dir: Path,
) -> None:
    (worktree / ".git").mkdir()
    code = (
        "from pathlib import Path\n"
        f"try:\n"
        f"    Path({str(worktree / '.git' / 'config')!r}).write_text('x')\n"
        "    print('WROTE')\n"
        "except OSError:\n"
        "    print('DENIED')\n"
    )
    settings_path = _settings_path(srt_path, tmp_path, fake_home, worktree, tmp_dir)

    result = await run_proc(
        wrap_argv(srt_path, settings_path, [PYTHON, "-c", code]),
        cwd=worktree,
        env={"HOME": str(tmp_dir), "TMPDIR": str(tmp_dir)},
        timeout_s=60,
    )

    assert result.ok, result.stderr
    assert "DENIED" in result.stdout


async def test_sandbox_blocks_reading_a_data_dir_outside_home(
    srt_path: Path,
    tmp_path: Path,
    fake_home: Path,
    worktree: Path,
    tmp_dir: Path,
) -> None:
    """Lead review 09a (S5): Gaia's database is unreadable even when GAIA_DATA_DIR is elsewhere."""
    data_dir = tmp_path / "gaia-data"
    data_dir.mkdir()
    (data_dir / "gaia.db").write_text("not for agents", encoding="utf-8")
    code = (
        "from pathlib import Path\n"
        "try:\n"
        f"    Path({str(data_dir / 'gaia.db')!r}).read_text()\n"
        "    print('READ')\n"
        "except OSError:\n"
        "    print('DENIED')\n"
    )
    spec = SandboxSpec(worktree=worktree, tmp_dir=tmp_dir, home=fake_home, data_dir=data_dir)
    settings_path = write_srt_settings(spec, tmp_path / "srt-settings")

    result = await run_proc(
        wrap_argv(srt_path, settings_path, [PYTHON, "-c", code]),
        cwd=worktree,
        env={"HOME": str(tmp_dir), "TMPDIR": str(tmp_dir)},
        timeout_s=60,
    )

    assert result.ok, result.stderr
    assert "DENIED" in result.stdout


async def test_sandbox_blocks_rewriting_a_worktree_git_file(
    srt_path: Path,
    tmp_path: Path,
    fake_home: Path,
    worktree: Path,
    tmp_dir: Path,
) -> None:
    """A real linked worktree has a ``.git`` *file*; rewriting it is finding F1's attack."""
    (worktree / ".git").write_text("gitdir: /somewhere/real\n", encoding="utf-8")
    code = (
        "from pathlib import Path\n"
        "try:\n"
        f"    Path({str(worktree / '.git')!r}).write_text('gitdir: /tmp/planted')\n"
        "    print('WROTE')\n"
        "except OSError:\n"
        "    print('DENIED')\n"
    )
    settings_path = _settings_path(srt_path, tmp_path, fake_home, worktree, tmp_dir)

    result = await run_proc(
        wrap_argv(srt_path, settings_path, [PYTHON, "-c", code]),
        cwd=worktree,
        env={"HOME": str(tmp_dir), "TMPDIR": str(tmp_dir)},
        timeout_s=60,
    )

    assert result.ok, result.stderr
    assert "DENIED" in result.stdout
    assert (worktree / ".git").read_text(encoding="utf-8") == "gitdir: /somewhere/real\n"
