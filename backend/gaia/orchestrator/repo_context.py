"""Build the static repository context a lead plan call can see (doc 11).

The repository is untrusted input. This module only reads it, and every path
the ticket names goes through :func:`gaia.security.paths.confine` before it is
opened. The result is plain text: the caller wraps it with
:func:`gaia.llm.untrusted.wrap_untrusted` before it reaches a model.
"""

from __future__ import annotations

import asyncio
import os
import re
from dataclasses import dataclass
from pathlib import Path

from gaia.domain.plans import TicketInput
from gaia.security.paths import PathDenied, confine

README_CAP_CHARS = 40_000
TREE_ENTRY_CAP = 2_000
FILE_CAP_CHARS = 40_000

#: Directories that are never useful to a lead plan and may contain secrets or
#: a huge number of generated files. The names are checked at every depth.
_SKIP_TREE_DIRS = frozenset({".git", ".venv", "node_modules", "__pycache__", "dist", "build"})

#: A path-like token: at least one dot extension, with optional ``/`` parts.
#: The lookahead keeps trailing punctuation and markdown characters out.
_PATH_TOKEN_RE = re.compile(
    r"(?<![\w])"
    r"(?P<path>[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*"
    r"\.(?:py|md|toml|ya?ml|json|txt|rs|js|ts|tsx|jsx|go|java|c|h|sh|sql|lock|"
    r"html|css|ini|cfg|rb|php|swift|kt|tf|mod|sum))"
    r"(?=[\s).\],;:'\"`<>]|$)"
)


@dataclass(frozen=True)
class RepoContext:
    """The repository information a lead plan call may use.

    ``files`` is ``(repo_relative_path, content)`` pairs for the ticket-named
    files that fit in the budget. ``omitted`` records every part that was
    dropped for space, but never records paths that the security policy denied.
    """

    readme: str | None
    tree: str
    files: tuple[tuple[str, str], ...]
    omitted: tuple[str, ...]

    def render(self) -> str:
        """Return the plain text for :func:`wrap_untrusted`."""
        sections: list[str] = []
        if self.readme is not None:
            sections.append(f"README\n=====\n{self.readme}")
        if self.tree:
            sections.append(f"Repository tree (depth 2)\n=====\n{self.tree}")
        for path, content in self.files:
            sections.append(f"File: {path}\n=====\n{content}")
        if self.omitted:
            sections.append("Omitted\n=====\n" + "\n".join(self.omitted))
        return "\n\n".join(sections)


async def build_repo_context(
    root: Path, ticket: TicketInput, *, budget_chars: int = 400_000
) -> RepoContext:
    """Build :class:`RepoContext` without blocking the event loop."""
    return await asyncio.to_thread(_build_repo_context_sync, root, ticket, budget_chars)


@dataclass(frozen=True)
class _TreeEntry:
    name: str
    is_dir: bool


def _build_repo_context_sync(root: Path, ticket: TicketInput, budget_chars: int) -> RepoContext:
    """Synchronous core of :func:`build_repo_context`."""
    omitted: list[str] = []
    used = 0

    def add(label: str, content: str) -> bool:
        nonlocal used
        if used + len(content) > budget_chars:
            omitted.append(f"{label} omitted (context budget)")
            return False
        used += len(content)
        return True

    readme: str | None = None
    readme_content = _read_root_readme(root)
    if readme_content is not None:
        content, truncated = readme_content
        if truncated:
            omitted.append(f"README truncated to {README_CAP_CHARS} characters")
        if not add("README", content):
            omitted.append("repository tree omitted (context budget)")
            omitted.append("ticket file contents omitted (context budget)")
            return RepoContext(None, "", (), tuple(omitted))
        readme = content

    tree, tree_omitted = _build_tree(root)
    omitted.extend(tree_omitted)
    if not add("repository tree", tree):
        omitted.append("ticket file contents omitted (context budget)")
        return RepoContext(readme, "", (), tuple(omitted))

    files: list[tuple[str, str]] = []
    candidates = _path_candidates(ticket)
    for index, candidate in enumerate(candidates):
        content_and_path = _read_ticket_file(root, candidate)
        if content_and_path is None:
            continue
        relative_path, content, truncated = content_and_path
        if truncated:
            omitted.append(f"file {relative_path} truncated to {FILE_CAP_CHARS} characters")
        if not add(f"file {relative_path}", content):
            remaining = len(candidates) - index
            if remaining > 1:
                omitted.append(f"{remaining - 1} ticket file contents omitted (context budget)")
            break
        files.append((relative_path, content))

    return RepoContext(readme, tree, tuple(files), tuple(omitted))


def _read_root_readme(root: Path) -> tuple[str, bool] | None:
    """Return the first root ``README*`` file, capped at :data:`README_CAP_CHARS`."""
    try:
        with os.scandir(root) as entries:
            readmes = sorted(
                (entry for entry in entries if entry.name.startswith("README")),
                key=lambda entry: entry.name,
            )
            first = readmes[0] if readmes else None
    except OSError:
        return None
    if first is None or not first.is_file():
        return None
    try:
        return _read_capped(Path(first.path), README_CAP_CHARS)
    except OSError:
        return None


def _read_capped(path: Path, cap: int) -> tuple[str, bool]:
    """Read ``path`` as UTF-8, capping it at ``cap`` characters."""
    text = path.read_text(encoding="utf-8", errors="replace")
    if len(text) > cap:
        return text[:cap], True
    return text, False


def _tree_entries(directory: Path) -> list[_TreeEntry]:
    """Sorted directory entries that are regular files or directories."""
    try:
        with os.scandir(directory) as scan:
            raw = list(scan)
    except OSError:
        return []
    raw.sort(key=lambda entry: entry.name)
    result: list[_TreeEntry] = []
    for entry in raw:
        if entry.name in _SKIP_TREE_DIRS:
            continue
        try:
            if entry.is_symlink():
                continue
            is_dir = entry.is_dir(follow_symlinks=False)
            is_file = entry.is_file(follow_symlinks=False)
        except OSError:
            continue
        if is_dir or is_file:
            result.append(_TreeEntry(name=entry.name, is_dir=is_dir))
    return result


def _build_tree(root: Path) -> tuple[str, list[str]]:
    """Return a depth-2 tree string and any truncation notes."""
    lines: list[str] = []
    omitted: list[str] = []
    count = 0
    truncated = False

    def add_entry(entry: _TreeEntry, indent: str) -> bool:
        nonlocal count, truncated
        if truncated:
            return False
        if count >= TREE_ENTRY_CAP:
            truncated = True
            omitted.append(f"repository tree truncated at {TREE_ENTRY_CAP} entries")
            return False
        suffix = "/" if entry.is_dir else ""
        lines.append(f"{indent}{entry.name}{suffix}")
        count += 1
        return True

    for entry in _tree_entries(root):
        if not add_entry(entry, ""):
            break
        if entry.is_dir:
            for child in _tree_entries(root / entry.name):
                if not add_entry(child, "  "):
                    break
    return "\n".join(lines), omitted


def _path_candidates(ticket: TicketInput) -> list[str]:
    """Return unique repo-relative path tokens found in the ticket text."""
    text = f"{ticket.title}\n{ticket.body}"
    candidates: list[str] = []
    for match in _PATH_TOKEN_RE.finditer(text):
        candidate = match.group("path")
        if candidate not in candidates:
            candidates.append(candidate)
    return candidates


def _read_ticket_file(root: Path, candidate: str) -> tuple[str, str, bool] | None:
    """Read one ticket-named file, or return ``None`` to skip it silently."""
    try:
        confined = confine(root, candidate)
    except (PathDenied, OSError):
        return None
    if not confined.is_file():
        return None
    root_path = root.resolve()
    try:
        relative_path = confined.relative_to(root_path).as_posix()
        content, truncated = _read_capped(confined, FILE_CAP_CHARS)
    except OSError:
        return None
    return relative_path, content, truncated
