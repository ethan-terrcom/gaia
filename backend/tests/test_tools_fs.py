"""Tests for the filesystem tools (doc 09b)."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any, cast

import pytest

from gaia.runtimes.approvals import InMemoryApprovalBroker
from gaia.tools import fs
from gaia.tools.registry import ToolContext, ToolRegistry
from tests.runtime_contract import wait_for_pending
from tests.tool_fakes import make_tool_context


@pytest.fixture
def worktree(tmp_path: Path) -> Path:
    path = tmp_path / "worktree"
    path.mkdir()
    return path


@pytest.fixture
def ctx(worktree: Path) -> ToolContext:
    return make_tool_context(worktree)


@pytest.fixture
def registry() -> ToolRegistry:
    built = ToolRegistry()
    built.register(fs.read_file)
    built.register(fs.list_dir)
    built.register(fs.write_file)
    built.register(fs.edit_file)
    built.register(fs.delete_file)
    built.register(fs.search(search_supports_regex=False))
    return built


_BAD_PATHS = ("../x", "/etc/passwd", ".git/config", "foo\x00bar")


@pytest.mark.parametrize("bad_path", _BAD_PATHS)
async def test_path_escapes_are_refused_for_every_fs_tool(
    registry: ToolRegistry, ctx: ToolContext, bad_path: str
) -> None:
    calls = {
        "read_file": {"path": bad_path},
        "list_dir": {"path": bad_path},
        "write_file": {"path": bad_path, "content": "x"},
        "edit_file": {"path": bad_path, "old": "a", "new": "b"},
        "delete_file": {"path": bad_path},
        "search": {"pattern": "x", "path": bad_path},
    }

    for tool_name, arguments in calls.items():
        result = await registry.call(tool_name, arguments, ctx, call_id=f"call-{tool_name}")
        assert result.ok is False, tool_name
        assert result.content.startswith("path denied: "), tool_name
        assert result.decision == "deny", tool_name


async def test_a_symlink_to_outside_is_refused_for_every_fs_tool(
    registry: ToolRegistry, ctx: ToolContext, worktree: Path, tmp_path: Path
) -> None:
    outside_dir = tmp_path / "outside-dir"
    outside_dir.mkdir()
    (outside_dir / "child.txt").write_text("secret", encoding="utf-8")
    (worktree / "link").symlink_to(outside_dir)
    calls = {
        "read_file": {"path": "link/child.txt"},
        "list_dir": {"path": "link"},
        "write_file": {"path": "link/new.txt", "content": "x"},
        "edit_file": {"path": "link/child.txt", "old": "a", "new": "b"},
        "delete_file": {"path": "link/child.txt"},
        "search": {"pattern": "secret", "path": "link"},
    }

    for tool_name, arguments in calls.items():
        result = await registry.call(tool_name, arguments, ctx, call_id=f"call-{tool_name}")
        assert result.ok is False, tool_name
        assert result.content.startswith("path denied: "), tool_name
        assert not (outside_dir / "new.txt").exists()


async def test_read_file_line_range(ctx: ToolContext, worktree: Path) -> None:
    (worktree / "a.txt").write_text("a\nb\nc\n", encoding="utf-8")

    result = await fs.read_file.handler(
        fs.ReadFileInput(path="a.txt", start_line=2, end_line=3), ctx
    )

    assert result.ok is True
    assert result.content == "b\nc"


async def test_read_file_binary_file(ctx: ToolContext, worktree: Path) -> None:
    (worktree / "blob").write_bytes(b"a\x00b")

    result = await fs.read_file.handler(fs.ReadFileInput(path="blob"), ctx)

    assert result.ok is True
    assert result.content == "binary file, 3 bytes"


async def test_read_file_over_cap_has_a_truncation_note(ctx: ToolContext, worktree: Path) -> None:
    (worktree / "big.txt").write_text("a" * (fs.READ_CAP_BYTES + 1), encoding="utf-8")

    result = await fs.read_file.handler(fs.ReadFileInput(path="big.txt"), ctx)

    assert result.ok is True
    assert result.content.startswith("a" * fs.READ_CAP_BYTES)
    assert result.content.endswith(
        f"\n[truncated: file is {fs.READ_CAP_BYTES + 1} bytes; use start_line/end_line]"
    )


async def test_list_dir_sorts_skips_ignored_and_does_not_follow_symlinks(
    ctx: ToolContext, worktree: Path, tmp_path: Path
) -> None:
    (worktree / "src" / "sub").mkdir(parents=True)
    (worktree / "src" / "a.py").write_text("a", encoding="utf-8")
    (worktree / "src" / "sub" / "b.py").write_text("b", encoding="utf-8")
    (worktree / "node_modules").mkdir()
    (worktree / "node_modules" / "ignored.txt").write_text("x", encoding="utf-8")
    outside = tmp_path / "outside-list"
    outside.mkdir()
    (worktree / "escape").symlink_to(outside)
    os.mkfifo(worktree / "pipe")

    result = await fs.list_dir.handler(fs.ListDirInput(path=".", depth=3), ctx)

    assert result.ok is True
    assert "src/" in result.content
    assert "src/a.py" in result.content
    assert "src/sub/" in result.content
    assert "src/sub/b.py" in result.content
    assert "escape -> (symlink)" in result.content
    assert "pipe" not in result.content
    assert "node_modules" not in result.content


async def test_list_dir_stops_at_depth(ctx: ToolContext, worktree: Path) -> None:
    (worktree / "a" / "b" / "c").mkdir(parents=True)
    (worktree / "a" / "b" / "c" / "deep.txt").write_text("x", encoding="utf-8")

    result = await fs.list_dir.handler(fs.ListDirInput(path=".", depth=2), ctx)

    assert result.ok is True
    assert "a/" in result.content
    assert "a/b/" in result.content
    assert "a/b/c/" not in result.content


async def test_write_file_creates_missing_parents(ctx: ToolContext, worktree: Path) -> None:
    result = await fs.write_file.handler(fs.WriteFileInput(path="a/b/c.txt", content="hello"), ctx)

    assert result.ok is True
    assert result.content == "wrote a/b/c.txt (5 bytes)"
    assert (worktree / "a" / "b" / "c.txt").read_text(encoding="utf-8") == "hello"


async def test_edit_file_rejects_zero_and_multiple_matches(
    ctx: ToolContext, worktree: Path
) -> None:
    (worktree / "a.txt").write_text("one one one", encoding="utf-8")

    zero = await fs.edit_file.handler(fs.EditFileInput(path="a.txt", old="missing", new="x"), ctx)
    assert zero.ok is False
    assert zero.content == "old text not found"

    multiple = await fs.edit_file.handler(fs.EditFileInput(path="a.txt", old="one", new="two"), ctx)
    assert multiple.ok is False
    assert multiple.content == "old text matches 3 times; include more context"


async def test_edit_file_replaces_one_exact_match(ctx: ToolContext, worktree: Path) -> None:
    (worktree / "a.txt").write_text("one two one", encoding="utf-8")

    result = await fs.edit_file.handler(fs.EditFileInput(path="a.txt", old="two", new="three"), ctx)

    assert result.ok is True
    assert (worktree / "a.txt").read_text(encoding="utf-8") == "one three one"


async def test_write_file_to_ci_workflow_asks_and_can_be_approved(
    registry: ToolRegistry, ctx: ToolContext, worktree: Path
) -> None:
    broker = cast(InMemoryApprovalBroker, ctx.approvals)
    request = asyncio.create_task(
        registry.call(
            "write_file",
            {"path": ".github/workflows/ci.yml", "content": "x"},
            ctx,
            call_id="call-approved",
        )
    )
    approval_id = await wait_for_pending(broker)
    broker.decide(approval_id, True, "ok")

    result = await asyncio.wait_for(request, timeout=5)

    assert result.ok is True
    assert result.decision == "ask_approved"
    assert (worktree / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8") == "x"


async def test_auto_mode_does_not_cover_a_protected_file_write(
    registry: ToolRegistry, worktree: Path
) -> None:
    # A hook or CI file can run code outside the sandbox, so it always asks.
    ctx = make_tool_context(worktree, auto_approve=lambda: True)
    broker = cast(InMemoryApprovalBroker, ctx.approvals)
    request = asyncio.create_task(
        registry.call(
            "write_file",
            {"path": ".github/workflows/ci.yml", "content": "x"},
            ctx,
            call_id="call-auto-protected",
        )
    )
    approval_id = await wait_for_pending(broker)
    broker.decide(approval_id, False, "no")

    result = await asyncio.wait_for(request, timeout=5)

    assert result.decision == "ask_rejected"
    assert not (worktree / ".github" / "workflows" / "ci.yml").exists()


async def test_write_file_to_ci_workflow_rejected_is_untouched(
    registry: ToolRegistry, ctx: ToolContext, worktree: Path
) -> None:
    broker = cast(InMemoryApprovalBroker, ctx.approvals)
    request = asyncio.create_task(
        registry.call(
            "write_file",
            {"path": ".github/workflows/ci.yml", "content": "x"},
            ctx,
            call_id="call-rejected",
        )
    )
    approval_id = await wait_for_pending(broker)
    broker.decide(approval_id, False, "no")

    result = await asyncio.wait_for(request, timeout=5)

    assert result.ok is False
    assert result.decision == "ask_rejected"
    assert result.content == "not approved: no"
    assert not (worktree / ".github" / "workflows" / "ci.yml").exists()


async def test_write_file_approval_timeout_counts_as_rejected(
    registry: ToolRegistry, worktree: Path
) -> None:
    ctx = make_tool_context(worktree, approval_timeout_s=0.05)

    result = await registry.call(
        "write_file",
        {"path": ".github/workflows/ci.yml", "content": "x"},
        ctx,
        call_id="call-timeout",
    )

    assert result.ok is False
    assert result.decision == "ask_rejected"
    assert "timed out" in result.content
    assert not (worktree / ".github" / "workflows" / "ci.yml").exists()


async def test_read_file_end_before_start_is_invalid(
    registry: ToolRegistry, ctx: ToolContext, worktree: Path
) -> None:
    (worktree / "a.txt").write_text("a\nb\n", encoding="utf-8")

    result = await registry.call(
        "read_file",
        {"path": "a.txt", "start_line": 3, "end_line": 2},
        ctx,
        call_id="call-bad-range",
    )

    assert result.ok is False
    assert "end_line must be greater than or equal to start_line" in result.content


async def test_read_file_directory_is_an_error(
    registry: ToolRegistry, ctx: ToolContext, worktree: Path
) -> None:
    (worktree / "subdir").mkdir()

    result = await registry.call("read_file", {"path": "subdir"}, ctx, call_id="call-read-dir")

    assert result.ok is False
    assert "not a regular file" in result.content


async def test_list_dir_file_is_an_error(
    registry: ToolRegistry, ctx: ToolContext, worktree: Path
) -> None:
    (worktree / "a.txt").write_text("x", encoding="utf-8")

    result = await registry.call("list_dir", {"path": "a.txt"}, ctx, call_id="call-list-file")

    assert result.ok is False
    assert "not a directory" in result.content


async def test_write_file_over_a_directory_is_an_error(
    registry: ToolRegistry, ctx: ToolContext, worktree: Path
) -> None:
    (worktree / "subdir").mkdir()

    result = await registry.call(
        "write_file",
        {"path": "subdir", "content": "x"},
        ctx,
        call_id="call-write-dir",
    )

    assert result.ok is False
    assert "cannot write over a directory" in result.content


async def test_edit_file_missing_file_is_an_error(registry: ToolRegistry, ctx: ToolContext) -> None:
    result = await registry.call(
        "edit_file",
        {"path": "missing.txt", "old": "a", "new": "b"},
        ctx,
        call_id="call-edit-missing",
    )

    assert result.ok is False
    assert "not a regular file" in result.content


async def test_delete_file_directory_is_an_error(
    registry: ToolRegistry, ctx: ToolContext, worktree: Path
) -> None:
    (worktree / "subdir").mkdir()

    result = await registry.call("delete_file", {"path": "subdir"}, ctx, call_id="call-delete-dir")

    assert result.ok is False
    assert "only regular files can be deleted" in result.content


async def test_edit_file_to_ci_workflow_asks_and_can_be_approved(
    registry: ToolRegistry, ctx: ToolContext, worktree: Path
) -> None:
    path = worktree / ".github" / "workflows" / "ci.yml"
    path.parent.mkdir(parents=True)
    path.write_text("old", encoding="utf-8")
    broker = cast(InMemoryApprovalBroker, ctx.approvals)
    request = asyncio.create_task(
        registry.call(
            "edit_file",
            {"path": ".github/workflows/ci.yml", "old": "old", "new": "new"},
            ctx,
            call_id="call-edit-approved",
        )
    )
    approval_id = await wait_for_pending(broker)
    broker.decide(approval_id, True, "ok")

    result = await asyncio.wait_for(request, timeout=5)

    assert result.ok is True
    assert result.decision == "ask_approved"
    assert path.read_text(encoding="utf-8") == "new"


async def test_edit_file_to_ci_workflow_rejected_is_untouched(
    registry: ToolRegistry, ctx: ToolContext, worktree: Path
) -> None:
    path = worktree / ".github" / "workflows" / "ci.yml"
    path.parent.mkdir(parents=True)
    path.write_text("old", encoding="utf-8")
    broker = cast(InMemoryApprovalBroker, ctx.approvals)
    request = asyncio.create_task(
        registry.call(
            "edit_file",
            {"path": ".github/workflows/ci.yml", "old": "old", "new": "new"},
            ctx,
            call_id="call-edit-rejected",
        )
    )
    approval_id = await wait_for_pending(broker)
    broker.decide(approval_id, False, "no")

    result = await asyncio.wait_for(request, timeout=5)

    assert result.ok is False
    assert result.decision == "ask_rejected"
    assert path.read_text(encoding="utf-8") == "old"


async def test_delete_file_to_ci_workflow_asks_and_can_be_approved(
    registry: ToolRegistry, ctx: ToolContext, worktree: Path
) -> None:
    path = worktree / ".github" / "workflows" / "ci.yml"
    path.parent.mkdir(parents=True)
    path.write_text("old", encoding="utf-8")
    broker = cast(InMemoryApprovalBroker, ctx.approvals)
    request = asyncio.create_task(
        registry.call(
            "delete_file",
            {"path": ".github/workflows/ci.yml"},
            ctx,
            call_id="call-delete-approved",
        )
    )
    approval_id = await wait_for_pending(broker)
    broker.decide(approval_id, True, "ok")

    result = await asyncio.wait_for(request, timeout=5)

    assert result.ok is True
    assert result.decision == "ask_approved"
    assert not path.exists()


async def test_delete_file_to_ci_workflow_rejected_is_untouched(
    registry: ToolRegistry, ctx: ToolContext, worktree: Path
) -> None:
    path = worktree / ".github" / "workflows" / "ci.yml"
    path.parent.mkdir(parents=True)
    path.write_text("old", encoding="utf-8")
    broker = cast(InMemoryApprovalBroker, ctx.approvals)
    request = asyncio.create_task(
        registry.call(
            "delete_file",
            {"path": ".github/workflows/ci.yml"},
            ctx,
            call_id="call-delete-rejected",
        )
    )
    approval_id = await wait_for_pending(broker)
    broker.decide(approval_id, False, "no")

    result = await asyncio.wait_for(request, timeout=5)

    assert result.ok is False
    assert result.decision == "ask_rejected"
    assert path.exists()


async def test_list_dir_stops_after_max_entries(
    registry: ToolRegistry, ctx: ToolContext, worktree: Path
) -> None:
    for index in range(fs.MAX_ENTRIES + 1):
        (worktree / f"f-{index:04d}.txt").write_text("x", encoding="utf-8")

    result = await registry.call("list_dir", {"path": "."}, ctx, call_id="call-list-cap")

    assert result.ok is True
    assert "[list truncated after 2000 entries]" in result.content
    assert result.data is not None
    assert result.data["truncated"] is True


async def test_python_fallback_skips_binary_oversize_and_caps_per_file(
    registry: ToolRegistry, worktree: Path
) -> None:
    ctx = make_tool_context(worktree, rg_path=None)
    (worktree / "binary.dat").write_bytes(b"needle\x00needle")
    (worktree / "big.txt").write_text("needle" * 200_000, encoding="utf-8")
    (worktree / "many.txt").write_text("needle\n" * 51, encoding="utf-8")

    result = await registry.call(
        "search", {"pattern": "needle", "path": "."}, ctx, call_id="call-python-caps"
    )

    assert result.ok is True
    assert "binary.dat" not in result.content
    assert "big.txt" not in result.content
    assert result.content.count("many.txt") == fs.SEARCH_MATCHES_PER_FILE


async def test_python_fallback_truncates_total_output(
    registry: ToolRegistry, worktree: Path
) -> None:
    ctx = make_tool_context(worktree, rg_path=None)
    line = ("a" * 300) + "needle\n"
    for index in range(6):
        (worktree / f"f-{index}.txt").write_text(line * 50, encoding="utf-8")

    result = await registry.call(
        "search", {"pattern": "needle", "path": "."}, ctx, call_id="call-python-trunc"
    )

    assert result.ok is True
    assert result.data is not None
    assert result.data["truncated"] is True
    assert result.content.endswith("[search truncated after 64000 characters]")


class _FakeSearchEntry:
    def __init__(
        self,
        name: str,
        *,
        is_dir: bool = False,
        is_file: bool = True,
        is_symlink: bool = False,
        size: int = 10,
        stat_error: bool = False,
    ) -> None:
        self.name = name
        self.path = f"/fake/{name}"
        self._is_dir = is_dir
        self._is_file = is_file
        self._is_symlink = is_symlink
        self._size = size
        self._stat_error = stat_error

    def is_dir(self, follow_symlinks: bool = False) -> bool:
        return self._is_dir

    def is_file(self, follow_symlinks: bool = False) -> bool:
        return self._is_file

    def is_symlink(self) -> bool:
        return self._is_symlink

    def stat(self, follow_symlinks: bool = False) -> Any:
        if self._stat_error:
            raise OSError("stat failed")
        return type("_Stat", (), {"st_size": self._size})()


class _FakeScandir:
    def __init__(self, entries: Any) -> None:
        self._entries = iter(entries)

    def __enter__(self) -> Any:
        return self._entries

    def __exit__(self, *args: Any) -> None:
        return None


def test_python_search_sync_handles_stat_and_read_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    stat_entry = _FakeSearchEntry("stat.txt", stat_error=True)
    monkeypatch.setattr(os, "scandir", lambda directory: _FakeScandir([stat_entry]))

    content, truncated = fs._python_search_sync(root, root, "needle", None)
    assert content == ""
    assert truncated is False

    read_entry = _FakeSearchEntry("read.txt", size=10)
    monkeypatch.setattr(os, "scandir", lambda directory: _FakeScandir([read_entry]))

    def raise_os_error(path: Path) -> bytes:
        raise OSError("read failed")

    monkeypatch.setattr(fs, "_read_no_follow", raise_os_error)
    content, truncated = fs._python_search_sync(root, root, "needle", None)
    assert content == ""
    assert truncated is False


async def test_line_range_reaches_past_the_first_200kb(ctx: ToolContext, worktree: Path) -> None:
    """Lead review F1: the truncation note says "use start_line/end_line", so a range
    beyond the first 200 KB must actually be readable."""
    lines = [f"line {number:06d} " + "x" * 40 for number in range(1, 20_001)]
    (worktree / "big.txt").write_text("\n".join(lines), encoding="utf-8")
    assert (worktree / "big.txt").stat().st_size > fs.READ_CAP_BYTES

    result = await fs.read_file.handler(
        fs.ReadFileInput(path="big.txt", start_line=19_999, end_line=20_000), ctx
    )

    assert result.ok is True
    assert result.content == "\n".join(lines[19_998:20_000])


async def test_line_range_output_is_capped(ctx: ToolContext, worktree: Path) -> None:
    (worktree / "wide.txt").write_text("y" * (fs.READ_CAP_BYTES + 10), encoding="utf-8")

    result = await fs.read_file.handler(
        fs.ReadFileInput(path="wide.txt", start_line=1, end_line=1), ctx
    )

    assert result.ok is True
    assert result.content.startswith("y" * fs.READ_CAP_BYTES)
    assert result.content.endswith("[truncated: the range is too long; read a smaller range]")


async def test_line_range_and_edit_refuse_huge_files(
    ctx: ToolContext, worktree: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(fs, "MAX_SCAN_BYTES", 10)
    (worktree / "huge.txt").write_text("z" * 11, encoding="utf-8")

    read = await fs.read_file.handler(fs.ReadFileInput(path="huge.txt", start_line=1), ctx)
    edit = await fs.edit_file.handler(fs.EditFileInput(path="huge.txt", old="z", new="q"), ctx)

    assert read.ok is False
    assert "too large to read by line range" in read.content
    assert edit.ok is False
    assert "too large to edit" in edit.content
    assert (worktree / "huge.txt").read_text(encoding="utf-8") == "z" * 11


async def test_errors_never_show_the_host_path(ctx: ToolContext, worktree: Path) -> None:
    (worktree / "adir").mkdir()

    result = await fs.read_file.handler(fs.ReadFileInput(path="adir"), ctx)

    assert result.ok is False
    assert str(worktree) not in result.content


async def test_read_file_returns_a_small_file_whole(ctx: ToolContext, worktree: Path) -> None:
    (worktree / "small.txt").write_text("one\ntwo\n", encoding="utf-8")

    result = await fs.read_file.handler(fs.ReadFileInput(path="small.txt"), ctx)

    assert result.ok is True
    assert result.content == "one\ntwo\n"
