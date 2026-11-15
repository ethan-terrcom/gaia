"""Tests for the search tool's rg mode and Python fallback (doc 09b)."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from gaia.config.settings import Settings
from gaia.tools import fs
from gaia.tools.registry import ToolRegistry
from tests.tool_fakes import make_tool_context


@pytest.fixture
def worktree(tmp_path: Path) -> Path:
    path = tmp_path / "worktree"
    path.mkdir()
    return path


@pytest.fixture
def registry() -> ToolRegistry:
    built = ToolRegistry()
    built.register(fs.search(search_supports_regex=True))
    return built


def _write_fake_rg(
    directory: Path,
    *,
    name: str,
    stdout: str = "",
    stderr: str = "",
    exit_code: int = 0,
) -> Path:
    script = directory / name
    record = directory / f"{name}-argv.json"
    code = (
        "#!/usr/bin/env python3\n"
        "import json, sys\n"
        "from pathlib import Path\n"
        f"Path({str(record)!r}).write_text(json.dumps(sys.argv))\n"
    )
    if stdout:
        code += f"print({stdout!r})\n"
    if stderr:
        code += f"print({stderr!r}, file=sys.stderr)\n"
    code += f"sys.exit({exit_code})\n"
    script.write_text(code, encoding="utf-8")
    script.chmod(0o755)
    return script


async def test_fake_rg_receives_the_exact_argv_and_returns_matches(
    registry: ToolRegistry, worktree: Path, tmp_path: Path
) -> None:
    fake = _write_fake_rg(
        tmp_path,
        name="rg-ok",
        stdout="src/a.py:1:hello world",
    )
    ctx = make_tool_context(worktree, rg_path=fake)
    (worktree / "src").mkdir()
    (worktree / "src" / "a.py").write_text("hello world\n", encoding="utf-8")

    result = await registry.call(
        "search",
        {"pattern": "a.c", "path": "src", "glob": "*.py"},
        ctx,
        call_id="call-rg",
    )

    assert result.ok is True
    assert result.content == "src/a.py:1:hello world\n"
    recorded = json.loads((tmp_path / "rg-ok-argv.json").read_text(encoding="utf-8"))
    assert recorded == [
        str(fake),
        "--no-config",
        "--line-number",
        "--no-heading",
        "--color=never",
        "--max-count=50",
        "--max-columns=300",
        "--max-filesize=1M",
        "--glob=!.git",
        "--glob",
        "*.py",
        "-e",
        "a.c",
        "--",
        "src",
    ]


async def test_fake_rg_exit_1_means_no_matches(
    registry: ToolRegistry, worktree: Path, tmp_path: Path
) -> None:
    fake = _write_fake_rg(tmp_path, name="rg-none", exit_code=1)
    ctx = make_tool_context(worktree, rg_path=fake)

    result = await registry.call(
        "search", {"pattern": "needle", "path": "."}, ctx, call_id="call-none"
    )

    assert result.ok is True
    assert result.content == ""


async def test_fake_rg_exit_2_is_an_error(
    registry: ToolRegistry, worktree: Path, tmp_path: Path
) -> None:
    fake = _write_fake_rg(tmp_path, name="rg-error", stderr="rg error", exit_code=2)
    ctx = make_tool_context(worktree, rg_path=fake)

    result = await registry.call(
        "search", {"pattern": "needle", "path": "."}, ctx, call_id="call-error"
    )

    assert result.ok is False
    assert result.content == "rg error"


async def test_python_fallback_is_fixed_string_not_regex(
    registry: ToolRegistry, worktree: Path
) -> None:
    ctx = make_tool_context(worktree, rg_path=None)
    (worktree / "one.txt").write_text("abc\n", encoding="utf-8")
    (worktree / "two.txt").write_text("a.c\n", encoding="utf-8")

    result = await registry.call(
        "search", {"pattern": "a.c", "path": "."}, ctx, call_id="call-python"
    )

    assert result.ok is True
    assert "one.txt" not in result.content
    assert "two.txt:1:a.c" in result.content


async def test_python_fallback_applies_glob_to_relative_paths(
    registry: ToolRegistry, worktree: Path
) -> None:
    ctx = make_tool_context(worktree, rg_path=None)
    (worktree / "keep.py").write_text("needle\n", encoding="utf-8")
    (worktree / "skip.txt").write_text("needle\n", encoding="utf-8")

    result = await registry.call(
        "search",
        {"pattern": "needle", "path": ".", "glob": "*.py"},
        ctx,
        call_id="call-glob",
    )

    assert result.ok is True
    assert "keep.py:1:needle" in result.content
    assert "skip.txt" not in result.content


async def test_a_glob_with_dotdot_is_refused(registry: ToolRegistry, worktree: Path) -> None:
    ctx = make_tool_context(worktree, rg_path=None)

    result = await registry.call(
        "search",
        {"pattern": "needle", "path": ".", "glob": "../*.py"},
        ctx,
        call_id="call-bad-glob",
    )

    assert result.ok is False
    assert result.content == "glob must not contain '..'"


@pytest.mark.parametrize("glob", ["/abs/*.py", "~/*.py", "bad\x00glob"])
async def test_absolute_home_and_nul_globs_are_refused(
    registry: ToolRegistry, worktree: Path, glob: str
) -> None:
    ctx = make_tool_context(worktree, rg_path=None)

    result = await registry.call(
        "search",
        {"pattern": "needle", "path": ".", "glob": glob},
        ctx,
        call_id="call-bad-glob-2",
    )

    assert result.ok is False
    assert result.content.startswith("glob must ")


async def test_fake_rg_proc_error_is_a_result(
    registry: ToolRegistry, worktree: Path, tmp_path: Path
) -> None:
    ctx = make_tool_context(worktree, rg_path=tmp_path)

    result = await registry.call(
        "search", {"pattern": "needle", "path": "."}, ctx, call_id="call-rg-proc-error"
    )

    assert result.ok is False
    assert result.content


async def test_python_fallback_skips_symlinks_ignored_dirs_and_handles_dirs(
    registry: ToolRegistry, worktree: Path, tmp_path: Path
) -> None:
    ctx = make_tool_context(worktree, rg_path=None)
    (worktree / "sub").mkdir()
    (worktree / "sub" / "keep.txt").write_text("needle\n", encoding="utf-8")
    (worktree / "node_modules").mkdir()
    (worktree / "node_modules" / "skip.txt").write_text("needle\n", encoding="utf-8")
    outside = tmp_path / "outside-search"
    outside.mkdir()
    (outside / "secret.txt").write_text("needle\n", encoding="utf-8")
    (worktree / "link").symlink_to(outside)
    os.mkfifo(worktree / "pipe")

    result = await registry.call(
        "search", {"pattern": "needle", "path": "."}, ctx, call_id="call-python-dirs"
    )

    assert result.ok is True
    assert "sub/keep.txt:1:needle" in result.content
    assert "node_modules" not in result.content
    assert "link" not in result.content
    assert "pipe" not in result.content


async def test_python_fallback_refuses_a_file_as_search_path(
    registry: ToolRegistry, worktree: Path
) -> None:
    (worktree / "a.txt").write_text("needle\n", encoding="utf-8")
    ctx = make_tool_context(worktree, rg_path=None)

    result = await registry.call(
        "search", {"pattern": "needle", "path": "a.txt"}, ctx, call_id="call-python-file"
    )

    assert result.ok is False
    assert "not a directory" in result.content


def test_find_rg_uses_explicit_path_and_agent_path(tmp_path: Path) -> None:
    rg = tmp_path / "rg"
    rg.write_text("#!/bin/sh\n", encoding="utf-8")
    rg.chmod(0o755)
    settings = Settings(data_dir=tmp_path / "data", rg_path=rg)

    assert fs.find_rg(settings) == rg

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    found = bin_dir / "rg"
    found.write_text("#!/bin/sh\n", encoding="utf-8")
    found.chmod(0o755)
    settings = Settings(data_dir=tmp_path / "data", agent_path=(str(bin_dir), "/usr/bin"))

    assert fs.find_rg(settings) == found


def test_find_rg_rejects_an_invalid_explicit_path(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path / "data", rg_path=tmp_path / "missing")

    with pytest.raises(ValueError, match="not an executable file"):
        fs.find_rg(settings)


def test_find_rg_returns_none_when_not_installed(tmp_path: Path) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    settings = Settings(data_dir=tmp_path / "data", agent_path=(str(empty),))

    assert fs.find_rg(settings) is None


def test_search_description_says_which_mode_is_active() -> None:
    regex = fs.search(search_supports_regex=True)
    literal = fs.search(search_supports_regex=False)

    assert "regular expression" in regex.description
    assert "literal string" in literal.description
