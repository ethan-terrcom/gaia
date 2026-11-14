"""Filesystem confinement for every path an agent can name (doc 09a tasks 1, 5).

A user path is a string from an agent or a model. It is untrusted. The two
entry points here turn it into a decision:

- :func:`confine` turns a relative path into an absolute one under a trusted
  root, or refuses it;
- :func:`classify_write` then says whether writing to that confined path may
  happen silently or must be approved first.

Both are pure functions so the architect can audit every rule against a test.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from pathlib import Path

from gaia.security.decision import Decision

#: The longest raw user input echoed into an error message. A denial reason
#: must never leak an entire file path or an attacker-controlled string into
#: the audit log.
_MAX_ECHOED_INPUT = 200

#: Windows drive prefix, e.g. ``C:`` or ``c:``. Path separators are irrelevant
#: for this check: ``C:\\x`` and ``C:/x`` must both be refused.
_WINDOWS_DRIVE = re.compile(r"^[A-Za-z]:")

#: Write targets that always require approval. Patterns are matched against
#: the confined path as a relative POSIX path, case-insensitively.
_ASK_WRITE_PATTERNS: tuple[tuple[str, str], ...] = (
    (".github/workflows/**", ".github/workflows/**"),
    ("**/package-lock.json", "package-lock.json"),
    ("**/pnpm-lock.yaml", "pnpm-lock.yaml"),
    ("**/yarn.lock", "yarn.lock"),
    ("**/uv.lock", "uv.lock"),
    ("**/poetry.lock", "poetry.lock"),
    ("**/*.lock", "**/*.lock"),
    ("**/.env", ".env at any depth"),
    ("**/.env.*", ".env.* at any depth"),
    ("**/.envrc", ".envrc at any depth"),
    (".pre-commit-config.yaml", ".pre-commit-config.yaml"),
    (".husky/**", ".husky/**"),
    (".gaia/**", ".gaia/**"),
    (".claude/**", ".claude/**"),
    (".cline/**", ".cline/**"),
    (".clinerules", ".clinerules"),
    (".mcp.json", ".mcp.json"),
    (".vscode/**", ".vscode/**"),
    (".idea/**", ".idea/**"),
)


class UnsafePath(ValueError):
    """A path resolved outside the roots Gaia allows (doc 05 task 4).

    Moved here from :mod:`gaia.services.git` in doc 09a so path confinement and
    the git service can share one exception without the security module
    importing the service.
    """


class PathDenied(UnsafePath):
    """:func:`confine` refused an untrusted user path."""


def confine(root: Path, user_path: str) -> Path:
    """Resolve ``user_path`` under ``root`` and refuse every escape.

    ``root`` must be an existing directory. ``user_path`` is always a plain
    relative POSIX path from an untrusted caller; it is never trusted to be
    relative, so absolute paths, home-relative paths, Windows drive/UNC paths,
    NUL bytes and the ``.git`` directory are all refused before it is joined.

    The joined path is resolved with symlinks followed, and the result must
    stay under the resolved root. In addition, every existing path component is
    checked individually: a symlink in a parent directory that points outside
    the root must be refused even when the final component is a plain file.
    """
    root_path = Path(root).resolve()
    if not root_path.is_dir():
        raise PathDenied(f"root {str(root_path)!r} is not an existing directory")

    if not isinstance(user_path, str):
        raise TypeError("user_path must be a str, not bytes or another type")
    if user_path == "":
        raise PathDenied("path is empty")
    if "\x00" in user_path:
        raise PathDenied(f"path contains a NUL byte: {_short_repr(user_path)}")
    if _is_absolute_or_home_relative(user_path):
        raise PathDenied(
            f"absolute, home-relative or Windows paths are not allowed: {_short_repr(user_path)}"
        )
    if _contains_dot_git_component(user_path):
        raise PathDenied(f"the .git directory is reserved: {_short_repr(user_path)}")

    candidate = root_path / Path(user_path)
    _refuse_symlink_escape(root_path, user_path)

    resolved = candidate.resolve(strict=False)
    if not resolved.is_relative_to(root_path):
        raise PathDenied(f"path resolves outside the root: {_short_repr(user_path)}")
    # The input check above only sees the spelling. A symlink inside the root
    # (``alias -> .git``, planted by a command the agent ran) resolves *into*
    # .git while every component of the input looks harmless, so check the
    # resolved location as well.
    if _contains_dot_git_component(resolved.relative_to(root_path).as_posix()):
        raise PathDenied(f"path resolves into the .git directory: {_short_repr(user_path)}")
    return resolved


def classify_write(root: Path, path: Path) -> Decision:
    """Classify a write to ``path``, which must already be confined to ``root``.

    The doc's write policy (task 5): CI workflow definitions, lockfiles, env
    files, hook configuration (``.pre-commit-config.yaml``, ``.husky/``, and
    direnv's ``.envrc``, which run code on the owner's machine) and the
    harness/agent config directories need approval, because writing them
    changes how *other* tools behave in the repository. Everything
    else is allowed. Matching is case-insensitive, because the worktree may sit
    on a case-insensitive APFS volume and ``.GIT`` must not bypass ``.git``.
    """
    root_path = Path(root).resolve()
    resolved = Path(path).resolve(strict=False)
    if not resolved.is_relative_to(root_path):
        raise PathDenied(f"path {str(resolved)!r} is outside root {str(root_path)!r}")
    if resolved == root_path:
        return Decision("allow", "allow: write to the worktree root")

    relative = resolved.relative_to(root_path).as_posix()
    folded = relative.casefold()
    for pattern, label in _ASK_WRITE_PATTERNS:
        if glob_match(folded, pattern.casefold()):
            return Decision("ask", f"ask: write to {label}")
    return Decision("allow", f"allow: write to {relative}")


def glob_match(path: str, pattern: str) -> bool:
    """Match a POSIX relative path against a small glob pattern.

    Supports ``*`` within a path component and ``**`` as a whole component
    matching zero or more components. This is intentionally not a general glob
    engine: the write policy only needs those two operators, and a hand-rolled
    matcher is auditable and dependency-free.
    """
    return _match_parts(path.split("/"), pattern.split("/"))


def _match_parts(parts: Sequence[str], patterns: Sequence[str]) -> bool:
    """Recursive core of :func:`glob_match`."""
    if not patterns:
        return not parts
    if patterns[0] == "**":
        # ``**`` may match zero components or one component plus the rest of
        # the pattern, so ``**/*.lock`` also matches ``foo.lock`` at the root.
        return _match_parts(parts, patterns[1:]) or (
            bool(parts) and _match_parts(parts[1:], patterns)
        )
    if not parts:
        return False
    if not _match_component(parts[0], patterns[0]):
        return False
    return _match_parts(parts[1:], patterns[1:])


def _match_component(name: str, pattern: str) -> bool:
    """Match one path component; ``*`` never crosses a ``/``."""
    if "*" not in pattern:
        return name == pattern
    regex = re.escape(pattern).replace(r"\*", "[^/]*")
    return re.fullmatch(regex, name) is not None


def _is_absolute_or_home_relative(user_path: str) -> bool:
    """Return True for a path that cannot be trusted as a relative POSIX path."""
    if user_path.startswith("~"):
        return True
    if _WINDOWS_DRIVE.match(user_path):
        return True
    if user_path.startswith("\\\\"):
        return True
    return Path(user_path).is_absolute()


def _contains_dot_git_component(user_path: str) -> bool:
    """Return True when any ``/``- or ``\\``-separated component is ``.git``.

    The comparison is ASCII case-insensitive on purpose: APFS is commonly
    case-insensitive, so ``.GIT/config`` names the same directory as
    ``.git/config`` and must be refused too.
    """
    components = (part for part in re.split(r"[\\/]+", user_path) if part not in ("", "."))
    return any(component.casefold() == ".git" for component in components)


def _refuse_symlink_escape(root_path: Path, user_path: str) -> None:
    """Refuse an existing symlink component that resolves outside ``root_path``.

    The final component alone is not enough: ``root/link/file`` has a symlink
    at ``link``, and ``file`` may not exist or may be a plain file inside the
    symlink target. Every prefix is checked from the root outward.
    """
    current = root_path
    for part in Path(user_path).parts:
        current = current / part
        if current.is_symlink():
            target = current.resolve(strict=False)
            if not target.is_relative_to(root_path):
                raise PathDenied(
                    f"path component is a symlink outside the root: {_short_repr(str(current))}"
                )


def _short_repr(value: str) -> str:
    """A repr of the first :data:`_MAX_ECHOED_INPUT` characters of ``value``."""
    return repr(value[:_MAX_ECHOED_INPUT])
