"""Filesystem tools every agent runtime offers (doc 09b task 3).

Every path argument is confined with :func:`gaia.security.paths.confine` before
it is used, and every write or delete is classified with
:func:`gaia.security.paths.classify_write` so sensitive files ask for approval.
Symlinks are never followed by the tools themselves: after ``confine`` resolves
the path, the file operations use ``O_NOFOLLOW`` so a symlink planted between
the check and the open cannot redirect the operation outside the worktree.
"""

from __future__ import annotations

import asyncio
import fnmatch
import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from pydantic import BaseModel, Field, model_validator

from gaia.config.settings import Settings
from gaia.security.paths import classify_write, confine
from gaia.services.proc import ProcError, base_environment, run_proc
from gaia.tools.registry import Tool, ToolContext, ToolInput, ToolResult, approve

#: Maximum bytes read by ``read_file``.
READ_CAP_BYTES = 200_000

#: Largest file ``read_file`` will scan for a line range, and ``edit_file`` will
#: load at all. A range past the first 200 KB must still be reachable (lead
#: review F1), but nothing loads an unbounded file into the server's memory.
MAX_SCAN_BYTES = 20_000_000

#: Maximum entries listed by ``list_dir``.
MAX_ENTRIES = 2_000

#: Maximum characters accepted by ``write_file``.
MAX_WRITE_CHARS = 1_000_000

#: Directories ``list_dir`` and the Python search fallback never descend into.
IGNORED_DIRS = frozenset(
    {
        ".git",
        "node_modules",
        ".venv",
        "venv",
        "__pycache__",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".tox",
        "dist",
        "build",
        ".next",
        "target",
        ".gradle",
        ".idea",
    }
)

#: Search output is capped at the same size as command output.
SEARCH_OUTPUT_CHARS = 64_000

#: Search stops after this many matches in one file.
SEARCH_MATCHES_PER_FILE = 50

#: Each search line is cut at this many characters.
SEARCH_LINE_CHARS = 300

#: Files larger than this are skipped by the Python search fallback.
SEARCH_MAX_FILE_BYTES = 1_000_000

#: Bytes inspected to decide whether a file is binary.
NUL_PROBE_BYTES = 8_192

_O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)


class FileToolError(Exception):
    """A file tool refused a request; the message is safe to show a model."""


@dataclass(frozen=True)
class _ReadFileResult:
    content: str
    size: int
    binary: bool


class ReadFileInput(ToolInput):
    path: str
    start_line: int | None = Field(default=None, ge=1)
    end_line: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def _end_after_start(self) -> ReadFileInput:
        if self.start_line is not None and self.end_line is not None:
            if self.end_line < self.start_line:
                raise ValueError("end_line must be greater than or equal to start_line")
        return self


class ListDirInput(ToolInput):
    path: str = "."
    depth: int = Field(default=1, ge=1, le=3)


class WriteFileInput(ToolInput):
    path: str
    content: str = Field(max_length=MAX_WRITE_CHARS)


class EditFileInput(ToolInput):
    path: str
    old: str = Field(min_length=1)
    new: str


class DeleteFileInput(ToolInput):
    path: str


class SearchInput(ToolInput):
    pattern: str = Field(min_length=1, max_length=500)
    path: str = "."
    glob: str | None = None


async def _read_file_handler(input: BaseModel, ctx: ToolContext) -> ToolResult:
    """Read a text file from the worktree, optionally a 1-based line range."""
    model = cast(ReadFileInput, input)
    path = confine(ctx.root, model.path)
    rel = _relative(ctx.root, path)
    try:
        read = await asyncio.to_thread(_read_file_sync, path, model.start_line, model.end_line)
    except FileToolError as exc:
        return ToolResult(False, str(exc), data={"target": rel, "reason": str(exc)})
    if read.binary:
        return ToolResult(
            True,
            read.content,
            data={"target": rel, "bytes": read.size, "binary": True},
        )
    return ToolResult(
        True,
        read.content,
        data={"target": rel, "bytes": read.size, "binary": False},
    )


async def _list_dir_handler(input: BaseModel, ctx: ToolContext) -> ToolResult:
    """List a directory as sorted, relative lines; symlinks are shown, not followed."""
    model = cast(ListDirInput, input)
    path = confine(ctx.root, model.path)
    rel = _relative(ctx.root, path)
    try:
        content, count, truncated = await asyncio.to_thread(
            _list_dir_sync, ctx.root, path, model.depth
        )
    except FileToolError as exc:
        return ToolResult(False, str(exc), data={"target": rel, "reason": str(exc)})
    return ToolResult(
        True,
        content,
        data={"target": rel, "entries": count, "truncated": truncated},
    )


async def _write_file_handler(input: BaseModel, ctx: ToolContext) -> ToolResult:
    """Write UTF-8 text to a path, creating missing parents inside the worktree."""
    model = cast(WriteFileInput, input)
    path = confine(ctx.root, model.path)
    rel = _relative(ctx.root, path)
    decision = classify_write(ctx.root, path)
    approved = decision.action == "allow"
    if decision.action == "ask":
        answer = await approve(ctx, {"path": rel}, decision.reason, tool="write_file")
        if not answer.approved:
            return ToolResult(
                False,
                f"not approved: {answer.reason}",
                decision="ask_rejected",
                data={"target": rel, "reason": answer.reason},
            )
        approved = True
    try:
        byte_count = await asyncio.to_thread(_write_file_sync, ctx.root, path, model.content)
    except FileToolError as exc:
        return ToolResult(False, str(exc), data={"target": rel, "reason": str(exc)})
    return ToolResult(
        True,
        f"wrote {rel} ({byte_count} bytes)",
        decision="ask_approved" if approved and decision.action == "ask" else "allow",
        data={"target": rel, "bytes": byte_count},
    )


async def _edit_file_handler(input: BaseModel, ctx: ToolContext) -> ToolResult:
    """Replace exactly one occurrence of ``old`` with ``new`` in a file."""
    model = cast(EditFileInput, input)
    path = confine(ctx.root, model.path)
    rel = _relative(ctx.root, path)
    decision = classify_write(ctx.root, path)
    approved = decision.action == "allow"
    if decision.action == "ask":
        answer = await approve(ctx, {"path": rel}, decision.reason, tool="edit_file")
        if not answer.approved:
            return ToolResult(
                False,
                f"not approved: {answer.reason}",
                decision="ask_rejected",
                data={"target": rel, "reason": answer.reason},
            )
        approved = True
    try:
        byte_count = await asyncio.to_thread(_edit_file_sync, path, model.old, model.new)
    except FileToolError as exc:
        return ToolResult(False, str(exc), data={"target": rel, "reason": str(exc)})
    return ToolResult(
        True,
        f"updated {rel}",
        decision="ask_approved" if approved and decision.action == "ask" else "allow",
        data={"target": rel, "bytes": byte_count},
    )


async def _delete_file_handler(input: BaseModel, ctx: ToolContext) -> ToolResult:
    """Delete a regular file from the worktree."""
    model = cast(DeleteFileInput, input)
    path = confine(ctx.root, model.path)
    rel = _relative(ctx.root, path)
    decision = classify_write(ctx.root, path)
    approved = decision.action == "allow"
    if decision.action == "ask":
        answer = await approve(ctx, {"path": rel}, decision.reason, tool="delete_file")
        if not answer.approved:
            return ToolResult(
                False,
                f"not approved: {answer.reason}",
                decision="ask_rejected",
                data={"target": rel, "reason": answer.reason},
            )
        approved = True
    try:
        await asyncio.to_thread(_delete_file_sync, path)
    except FileToolError as exc:
        return ToolResult(False, str(exc), data={"target": rel, "reason": str(exc)})
    return ToolResult(
        True,
        f"deleted {rel}",
        decision="ask_approved" if approved and decision.action == "ask" else "allow",
        data={"target": rel},
    )


async def _search_handler(input: BaseModel, ctx: ToolContext) -> ToolResult:
    """Run the rg search, or the fixed-string Python fallback when rg is absent."""
    model = cast(SearchInput, input)
    try:
        _validate_glob(model.glob)
    except FileToolError as exc:
        return ToolResult(False, str(exc), data={"reason": str(exc)})

    path = confine(ctx.root, model.path)
    rel = _relative(ctx.root, path)
    if ctx.rg_path is not None:
        return await _search_with_rg(ctx, model, path, rel)
    return await _search_with_python(ctx, model, path, rel)


async def _search_with_rg(ctx: ToolContext, input: SearchInput, path: Path, rel: str) -> ToolResult:
    glob_args: list[str] = ["--glob", input.glob] if input.glob is not None else []
    argv = [
        str(ctx.rg_path),
        "--no-config",
        "--line-number",
        "--no-heading",
        "--color=never",
        "--max-count=50",
        "--max-columns=300",
        "--max-filesize=1M",
        "--glob=!.git",
        *glob_args,
        "-e",
        input.pattern,
        "--",
        rel,
    ]
    env = base_environment()
    env.pop("RIPGREP_CONFIG_PATH", None)
    try:
        result = await run_proc(
            argv,
            cwd=ctx.root,
            timeout_s=30,
            env=env,
            max_output_bytes=SEARCH_OUTPUT_CHARS,
        )
    except ProcError as exc:
        return ToolResult(False, str(exc), data={"target": rel, "reason": str(exc)})
    if result.timed_out or result.returncode not in (0, 1):
        first_line = result.stderr.strip().splitlines()
        return ToolResult(
            False,
            first_line[0] if first_line else "search failed",
            data={"target": rel, "reason": first_line[0] if first_line else "search failed"},
        )
    return ToolResult(
        True,
        result.stdout,
        data={"target": rel, "mode": "rg", "matches_truncated": result.stdout_truncated},
    )


async def _search_with_python(
    ctx: ToolContext, input: SearchInput, path: Path, rel: str
) -> ToolResult:
    try:
        content, truncated = await asyncio.to_thread(
            _python_search_sync, ctx.root, path, input.pattern, input.glob
        )
    except FileToolError as exc:
        return ToolResult(False, str(exc), data={"target": rel, "reason": str(exc)})
    return ToolResult(
        True,
        content,
        data={"target": rel, "mode": "python", "truncated": truncated},
    )


def _read_file_sync(path: Path, start_line: int | None, end_line: int | None) -> _ReadFileResult:
    if not path.is_file():
        raise FileToolError("not a regular file")
    ranged = start_line is not None or end_line is not None
    fd = os.open(path, os.O_RDONLY | _O_NOFOLLOW)
    try:
        size = os.fstat(fd).st_size
        if ranged and size > MAX_SCAN_BYTES:
            raise FileToolError(f"file is {size} bytes; too large to read by line range")
        data = _read_fd(fd) if ranged else os.read(fd, READ_CAP_BYTES)
    finally:
        os.close(fd)

    if b"\x00" in data[:NUL_PROBE_BYTES]:
        return _ReadFileResult(content=f"binary file, {size} bytes", size=size, binary=True)

    text = data.decode("utf-8", errors="replace")
    if not ranged:
        if size > READ_CAP_BYTES:
            text += f"\n[truncated: file is {size} bytes; use start_line/end_line]"
        return _ReadFileResult(content=text, size=size, binary=False)
    selected = _select_lines(text, start_line, end_line)
    if len(selected) > READ_CAP_BYTES:
        selected = (
            selected[:READ_CAP_BYTES] + "\n[truncated: the range is too long; read a smaller range]"
        )
    return _ReadFileResult(content=selected, size=size, binary=False)


def _read_fd(fd: int) -> bytes:
    """Read ``fd`` to the end, never more than ``MAX_SCAN_BYTES``.

    A buffered reader loops over short reads itself. ``closefd=False`` leaves
    closing to the caller, which opened it with ``O_NOFOLLOW``.
    """
    with os.fdopen(fd, "rb", closefd=False) as handle:
        return handle.read(MAX_SCAN_BYTES)


def _select_lines(text: str, start_line: int | None, end_line: int | None) -> str:
    lines = text.splitlines()
    start = 1 if start_line is None else start_line
    end = len(lines) if end_line is None else end_line
    return "\n".join(lines[start - 1 : end])


def _list_dir_sync(root: Path, path: Path, depth: int) -> tuple[str, int, bool]:
    if not path.is_dir():
        raise FileToolError("not a directory")
    lines: list[str] = []
    truncated = False

    def walk(directory: Path, prefix: str) -> None:
        nonlocal truncated
        with os.scandir(directory) as entries:
            for entry in sorted(entries, key=lambda item: item.name):
                if len(lines) >= MAX_ENTRIES:
                    truncated = True
                    return
                rel = f"{prefix}/{entry.name}" if prefix else entry.name
                if entry.is_symlink():
                    lines.append(f"{rel} -> (symlink)")
                    continue
                if entry.is_dir(follow_symlinks=False):
                    if entry.name in IGNORED_DIRS:
                        continue
                    lines.append(f"{rel}/")
                    if len(Path(rel).parts) < depth:
                        walk(Path(entry.path), rel)
                    continue
                if entry.is_file(follow_symlinks=False):
                    size = entry.stat(follow_symlinks=False).st_size
                    lines.append(f"{rel} ({size} B)")

    walk(path, "")
    content = "\n".join(lines)
    if truncated:
        content += "\n[list truncated after 2000 entries]"
    return content, len(lines), truncated


def _write_file_sync(root: Path, path: Path, content: str) -> int:
    if path.is_dir():
        raise FileToolError("cannot write over a directory")
    _mkdir_confined_parents(root, path)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | _O_NOFOLLOW, 0o644)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(content)
    return len(content.encode("utf-8"))


def _mkdir_confined_parents(root: Path, path: Path) -> None:
    """Create missing parents, confining each one before it is created."""
    missing: list[Path] = []
    parent = path.parent
    while parent != root and not parent.exists():
        missing.append(parent)
        parent = parent.parent
    for directory in reversed(missing):
        confine(root, str(directory.relative_to(root)))
    path.parent.mkdir(parents=True, exist_ok=True)


def _edit_file_sync(path: Path, old: str, new: str) -> int:
    if not path.is_file():
        raise FileToolError("not a regular file")
    size = path.stat().st_size
    if size > MAX_SCAN_BYTES:
        raise FileToolError(f"file is {size} bytes; too large to edit")
    data = _read_no_follow(path)
    old_bytes = old.encode("utf-8")
    new_bytes = new.encode("utf-8")
    count = data.count(old_bytes)
    if count == 0:
        raise FileToolError("old text not found")
    if count > 1:
        raise FileToolError(f"old text matches {count} times; include more context")
    replaced = data.replace(old_bytes, new_bytes, 1)
    _write_no_follow(path, replaced)
    return len(replaced)


def _delete_file_sync(path: Path) -> None:
    if not path.is_file():
        raise FileToolError("only regular files can be deleted")
    path.unlink()


def _read_no_follow(path: Path) -> bytes:
    """Read a whole file without following a symlink planted after ``confine``."""
    fd = os.open(path, os.O_RDONLY | _O_NOFOLLOW)
    try:
        chunks: list[bytes] = []
        while True:
            chunk = os.read(fd, READ_CAP_BYTES)
            if not chunk:
                break
            chunks.append(chunk)
        return b"".join(chunks)
    finally:
        os.close(fd)


def _write_no_follow(path: Path, data: bytes) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_TRUNC | _O_NOFOLLOW)
    try:
        os.write(fd, data)
    finally:
        os.close(fd)


def _python_search_sync(root: Path, path: Path, pattern: str, glob: str | None) -> tuple[str, bool]:
    if not path.is_dir():
        raise FileToolError("not a directory")
    lines: list[str] = []
    total_chars = 0
    truncated = False

    def walk(directory: Path, prefix: str) -> None:
        nonlocal total_chars, truncated
        with os.scandir(directory) as entries:
            for entry in sorted(entries, key=lambda item: item.name):
                rel = f"{prefix}/{entry.name}" if prefix else entry.name
                if entry.is_symlink():
                    continue
                if entry.is_dir(follow_symlinks=False):
                    if entry.name in IGNORED_DIRS:
                        continue
                    walk(Path(entry.path), rel)
                    continue
                if not entry.is_file(follow_symlinks=False):
                    continue
                if glob is not None and not fnmatch.fnmatch(rel, glob):
                    continue
                try:
                    size = entry.stat(follow_symlinks=False).st_size
                except OSError:
                    continue
                if size > SEARCH_MAX_FILE_BYTES:
                    continue
                try:
                    data = _read_no_follow(Path(entry.path))
                except OSError:
                    continue
                if b"\x00" in data[:NUL_PROBE_BYTES]:
                    continue
                text = data.decode("utf-8", errors="replace")
                matches = 0
                for line_number, line in enumerate(text.splitlines(), start=1):
                    if pattern not in line:
                        continue
                    matches += 1
                    if matches > SEARCH_MATCHES_PER_FILE:
                        break
                    rendered = f"{rel}:{line_number}:{line[:SEARCH_LINE_CHARS]}"
                    if total_chars + len(rendered) + 1 > SEARCH_OUTPUT_CHARS:
                        truncated = True
                        return
                    lines.append(rendered)
                    total_chars += len(rendered) + 1

    walk(path, "")
    content = "\n".join(lines)
    if truncated:
        content += "\n[search truncated after 64000 characters]"
    return content, truncated


def _validate_glob(glob: str | None) -> None:
    if glob is None:
        return
    if "\x00" in glob:
        raise FileToolError("glob must not contain a NUL byte")
    if glob.startswith("~") or Path(glob).is_absolute():
        raise FileToolError("glob must be a relative path")
    if ".." in Path(glob).parts:
        raise FileToolError("glob must not contain '..'")


def _relative(root: Path, path: Path) -> str:
    if path == root:
        return "."
    return path.relative_to(root).as_posix()


def _search_description(*, search_supports_regex: bool) -> str:
    mode = (
        "the pattern is a regular expression (ripgrep syntax)"
        if search_supports_regex
        else "the pattern is a literal string (exact substring match)"
    )
    return (
        f"Search files under a directory in the worktree. {mode}. "
        "Returns rg-style rel/path:LINE:text lines, capped at 50 matches per file "
        "and 64000 characters. Use the glob argument to filter by filename."
    )


def search(*, search_supports_regex: bool) -> Tool:
    """The ``search`` tool, with its mode baked into the description."""
    return Tool(
        name="search",
        description=_search_description(search_supports_regex=search_supports_regex),
        input_model=SearchInput,
        handler=_search_handler,
        mutates=False,
    )


def find_rg(settings: Settings) -> Path | None:
    """Locate ripgrep from settings, never from the ambient process ``PATH``."""
    if settings.rg_path is not None:
        candidate = Path(settings.rg_path).expanduser()
        if not candidate.is_file() or not os.access(candidate, os.X_OK):
            raise ValueError(f"rg_path {str(candidate)!r} is not an executable file")
        return candidate
    found = shutil.which("rg", path=os.pathsep.join(settings.agent_path))
    return Path(found) if found else None


read_file = Tool(
    name="read_file",
    description=(
        "Read a UTF-8 text file from the worktree. Paths are relative to the "
        "worktree root. Use start_line/end_line for a 1-based inclusive range."
    ),
    input_model=ReadFileInput,
    handler=_read_file_handler,
    mutates=False,
)

list_dir = Tool(
    name="list_dir",
    description=(
        "List a directory in the worktree, sorted by name, one entry per line. "
        "Depth is 1-3. Symlinks are shown but never followed."
    ),
    input_model=ListDirInput,
    handler=_list_dir_handler,
    mutates=False,
)

write_file = Tool(
    name="write_file",
    description=(
        "Write UTF-8 text to a file in the worktree, creating missing parent "
        "directories. Existing directories are refused."
    ),
    input_model=WriteFileInput,
    handler=_write_file_handler,
    mutates=True,
)

edit_file = Tool(
    name="edit_file",
    description=(
        "Replace exactly one occurrence of old with new in an existing file. "
        "Zero or multiple matches are refused."
    ),
    input_model=EditFileInput,
    handler=_edit_file_handler,
    mutates=True,
)

delete_file = Tool(
    name="delete_file",
    description="Delete a regular file from the worktree. Directories are refused.",
    input_model=DeleteFileInput,
    handler=_delete_file_handler,
    mutates=True,
)
