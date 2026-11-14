"""Throwaway git repositories for the doc 05 tests.

Doc 05's tests are about real git behaviour - a hook that must not run, a
worktree that must refuse to disappear, a config file that must win - so they use
real ``git`` against a repository in ``tmp_path``. Only the ``live`` marker keeps
tests off the network; spawning ``git`` is what these tests are *for*, and
``[tool.ruff.lint.per-file-ignores]`` allows ``subprocess`` under ``tests/``.

The fixture repository is built with its own environment
(:data:`FIXTURE_GIT_ENV`): the owner's global config is not read, so a developer
with ``commit.gpgsign=true`` or a husky template does not get a different test
result from CI.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

#: A fixed identity for fixture commits, so no test depends on who is running it.
FIXTURE_NAME = "Gaia Test"
FIXTURE_EMAIL = "test@example.invalid"

#: The environment fixture git runs in: no system or global config, no prompts.
FIXTURE_GIT_ENV = {
    "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
    "HOME": os.environ.get("HOME", "/tmp"),
    "LANG": "C.UTF-8",
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_TERMINAL_PROMPT": "0",
}


def resolved(path: Path) -> Path:
    """``Path.resolve()`` outside a coroutine (ruff's ASYNC240 keeps it there).

    Shared with the test modules, which assert against resolved paths inside
    ``async`` tests.
    """
    return path.resolve()


@dataclass(frozen=True)
class OnDisk:
    """What the filesystem says about one path, asked outside a coroutine."""

    exists: bool
    is_dir: bool
    is_file: bool


def on_disk(path: Path | str) -> OnDisk:
    """Stat ``path`` in a plain function, for the same reason as :func:`resolved`."""
    candidate = Path(path)
    return OnDisk(exists=candidate.exists(), is_dir=candidate.is_dir(), is_file=candidate.is_file())


def git(cwd: Path, *args: str) -> str:
    """Run one git command in ``cwd`` and return its stdout.

    Failing here is a broken fixture, not a failing assertion, so it raises
    ``AssertionError`` with git's stderr rather than returning a code.
    """
    completed = subprocess.run(
        ["git", *args],
        cwd=cwd,
        env=FIXTURE_GIT_ENV,
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed: {completed.stderr}")
    return completed.stdout


def make_repo(path: Path) -> Path:
    """Create a repository with one commit on ``main``."""
    path.mkdir(parents=True, exist_ok=True)
    git(path, "init", "-q", "-b", "main")
    git(path, "config", "user.name", FIXTURE_NAME)
    git(path, "config", "user.email", FIXTURE_EMAIL)
    (path / "README.md").write_text("# fixture\n")
    git(path, "add", "--all")
    git(path, "commit", "-q", "-m", "initial commit")
    return path


def commit_file(repo: Path, name: str, content: str, message: str) -> str:
    """Write ``name``, commit it, and return the new SHA."""
    (repo / name).write_text(content)
    git(repo, "add", "--all")
    git(repo, "commit", "-q", "-m", message)
    return git(repo, "rev-parse", "HEAD").strip()
