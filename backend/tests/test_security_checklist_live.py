"""Live ``srt`` tests for the doc 18d security checklist.

These tests are skipped unless ``GAIA_SRT_PATH`` points at the pinned
``sandbox-runtime`` executable.  The keychain test is additionally macOS-only and
uses only a throwaway keychain inside a fake home; it never touches the login
keychain or any real item.
"""

from __future__ import annotations

import os
import secrets
import sys
from pathlib import Path

import pytest

from gaia.config.settings import Settings
from gaia.security.sandbox import SandboxSpec, wrap_argv, write_srt_settings
from gaia.services.proc import run_proc
from gaia.tools.shell import SrtCommandRunner

_GAIA_SRT_PATH = os.environ.get("GAIA_SRT_PATH")
_SRT_AVAILABLE = bool(_GAIA_SRT_PATH and Path(_GAIA_SRT_PATH).is_file())

_PYTHON = sys.executable
_SECURITY = "/usr/bin/security"


def _srt_path() -> Path:
    assert _GAIA_SRT_PATH is not None
    return Path(_GAIA_SRT_PATH)


@pytest.mark.skipif(
    not _SRT_AVAILABLE,
    reason="GAIA_SRT_PATH must point at the pinned srt executable",
)
async def test_production_srt_command_runner_blocks_conftest_escape(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The two-step ``conftest.py`` + ``pytest`` attack fails through production runner."""
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    task_dir = tmp_path / "task-tmp"
    task_dir.mkdir()
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    (fake_home / ".ssh").mkdir(parents=True)
    key_path = fake_home / ".ssh" / "id_18d"
    key_path.write_text("fake-secret-key", encoding="utf-8")

    # ``SandboxSpec`` defaults to ``Path.home()``; point HOME at the fake home so
    # the production defaults never read or write the real home directory.
    monkeypatch.setenv("HOME", str(fake_home))
    settings = Settings(data_dir=tmp_path / "gaia-data")
    runner = SrtCommandRunner(settings, srt_path=_srt_path())

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

    result = await runner.run(
        [_PYTHON, "-m", "pytest", "-q", "test_escape.py"],
        cwd=worktree,
        timeout_s=60,
        task_dir=task_dir,
    )

    assert result.ok, result.stderr


@pytest.mark.skipif(
    not _SRT_AVAILABLE or sys.platform != "darwin" or not Path(_SECURITY).is_file(),
    reason="requires GAIA_SRT_PATH, macOS and /usr/bin/security",
)
async def test_srt_process_cannot_read_throwaway_keychain(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A process under ``srt`` cannot print an item from a throwaway keychain."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    tmp_dir = tmp_path / "task-tmp"
    tmp_dir.mkdir()

    keychain = fake_home / "k.keychain-db"
    password = secrets.token_urlsafe(24)
    service = "gaia-18d-keychain-test"
    account = "gaia-18d"
    dummy_value = "18d-throwaway-keychain-value"

    async def security(*args: str) -> None:
        result = await run_proc(
            [_SECURITY, *args],
            cwd=tmp_path,
            timeout_s=60,
            env={"HOME": str(fake_home), "TMPDIR": str(tmp_dir)},
        )
        assert result.ok, result.stderr

    await security("create-keychain", "-p", password, str(keychain))
    try:
        await security(
            "add-generic-password",
            "-a",
            account,
            "-s",
            service,
            "-w",
            dummy_value,
            "-T",
            _SECURITY,
            str(keychain),
        )

        monkeypatch.setenv("HOME", str(fake_home))
        spec = SandboxSpec(worktree=worktree, tmp_dir=tmp_dir, home=fake_home)
        settings_path = write_srt_settings(spec, tmp_path / "srt-settings")
        result = await run_proc(
            wrap_argv(
                _srt_path(),
                settings_path,
                [_SECURITY, "find-generic-password", "-s", service, "-w", str(keychain)],
            ),
            cwd=worktree,
            env={"HOME": str(tmp_dir), "TMPDIR": str(tmp_dir)},
            timeout_s=60,
        )

        assert dummy_value not in result.stdout
        assert dummy_value not in result.stderr
    finally:
        await security("delete-keychain", str(keychain))


@pytest.mark.skipif(
    not _SRT_AVAILABLE,
    reason="GAIA_SRT_PATH must point at the pinned srt executable",
)
async def test_production_srt_env_has_no_secret_names(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``/usr/bin/env`` under the production runner shows no KEY/TOKEN/SECRET name."""
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    task_dir = tmp_path / "task-tmp"
    task_dir.mkdir()
    fake_home = tmp_path / "home"
    fake_home.mkdir()

    monkeypatch.setenv("HOME", str(fake_home))
    settings = Settings(data_dir=tmp_path / "gaia-data")
    runner = SrtCommandRunner(settings, srt_path=_srt_path())

    result = await runner.run(
        ["/usr/bin/env"],
        cwd=worktree,
        timeout_s=30,
        task_dir=task_dir,
    )

    assert result.ok, result.stderr
    for line in result.stdout.splitlines():
        if "=" not in line:
            continue
        name = line.split("=", 1)[0]
        for marker in ("KEY", "TOKEN", "SECRET"):
            assert marker not in name, f"{name!r} leaked into the sandboxed environment"
