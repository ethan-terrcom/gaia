"""OS sandbox profile for agent-spawned processes (doc 09a task 6, D1).

The command policy is only a tripwire. The boundary is Anthropic's
``sandbox-runtime`` (``srt``), launched as ``srt --settings <file.json> -- <cmd>``.
This module builds that settings file as a pure function of a
:class:`SandboxSpec`, writes it with owner-only permissions, and locates the
pinned ``srt`` binary.

The rules here are deliberately boring and explicit: reads come from a small
allowlist, writes are denied by default and only the worktree/temp dir plus a
few caller-supplied paths are allowed, and every escape hatch in the srt schema
is set to false.
"""

from __future__ import annotations

import json
import os
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from gaia.config.settings import Settings

#: Sensitive directories and files under ``home`` that the sandbox must never
#: read, listed in denyRead as defence in depth on top of denying all of home.
_SENSITIVE_RELATIVE: tuple[str, ...] = (
    ".ssh",
    ".aws",
    ".config",
    ".gnupg",
    "Library/Keychains",
    ".gaia",
    ".claude",
    ".cline",
    ".netrc",
    ".git-credentials",
    ".docker",
    ".kube",
    ".npmrc",
    ".pypirc",
)

#: Shell startup files. A shell that runs inside the sandbox must not be able
#: to source the owner's profiles, which commonly load secrets or extend PATH.
_SHELL_PROFILE_RELATIVE: tuple[str, ...] = (
    ".bashrc",
    ".bash_profile",
    ".profile",
    ".zshrc",
    ".zprofile",
    ".zshenv",
    ".zlogin",
    ".config/fish/config.fish",
)


class SandboxConfigError(ValueError):
    """The requested sandbox settings would weaken the security boundary."""


class SandboxUnavailable(RuntimeError):
    """The pinned ``srt`` binary could not be found."""


class SandboxSpec(BaseModel):
    """Everything that differs between two sandboxed commands.

    ``home`` is injectable for tests; production uses :func:`Path.home`. All
    paths must be absolute by the time :func:`build_srt_settings` runs; a path
    that still starts with ``~`` is expanded there.

    ``data_dir`` is Gaia's own data directory (``Settings.data_dir``: the
    database, checkpoints, logs). It is denied explicitly because it need not
    live under ``home`` (``GAIA_DATA_DIR`` can point anywhere), and a
    sandboxed command must never read Gaia's database. Callers should always
    pass it; the worktree may live inside it.
    """

    model_config = ConfigDict(frozen=True)

    worktree: Path
    tmp_dir: Path
    home: Path = Field(default_factory=Path.home)
    allowed_domains: tuple[str, ...] = ()
    gateway: str | None = None
    toolchain_read: tuple[Path, ...] = ()
    extra_write: tuple[Path, ...] = ()
    data_dir: Path | None = None


def build_srt_settings(spec: SandboxSpec) -> dict[str, Any]:
    """Build the srt settings dict for ``spec``.

    Pure by design: the same spec always produces the same JSON, so a test can
    assert every rule.
    """
    home = _absolute(spec.home)
    worktree = _absolute(spec.worktree)
    tmp_dir = _absolute(spec.tmp_dir)
    toolchain_read = _existing_absolute_paths(spec.toolchain_read)
    extra_write = _absolute_paths(spec.extra_write)

    allow_read = _dedupe_paths((worktree, tmp_dir, *toolchain_read))
    allow_write = _dedupe_paths((worktree, tmp_dir, *extra_write))
    data_dir = _absolute(spec.data_dir) if spec.data_dir is not None else None
    sensitive_paths = _sensitive_paths(home, data_dir)

    _validate_allow_entries(allow_read, allow_write, home, sensitive_paths)
    if data_dir is not None:
        _validate_data_dir_entries((*allow_read, *allow_write), data_dir)

    deny_read = _dedupe_paths((home, *sensitive_paths))
    deny_write = _deny_write_entries(worktree)
    allowed_domains = _network_domains(spec.allowed_domains, spec.gateway)

    return {
        "network": {
            "allowedDomains": allowed_domains,
            "deniedDomains": [],
            "allowLocalBinding": False,
            "allowUnixSockets": [],
        },
        "filesystem": {
            "denyRead": [str(path) for path in deny_read],
            "allowRead": [str(path) for path in allow_read],
            "allowWrite": [str(path) for path in allow_write],
            "denyWrite": deny_write,
        },
        "allowPty": False,
        "allowAppleEvents": False,
        "enableWeakerNestedSandbox": False,
        "enableWeakerNetworkIsolation": False,
    }


def write_srt_settings(spec: SandboxSpec, directory: Path) -> Path:
    """Write the settings JSON for ``spec`` under ``directory``, mode ``0o600``."""
    directory_path = Path(directory)
    if directory_path.is_file():
        raise SandboxConfigError(
            f"srt settings directory {str(directory_path)!r} is not a directory"
        )
    directory_path.mkdir(parents=True, exist_ok=True)
    settings = build_srt_settings(spec)
    path = directory_path / "srt-settings.json"
    path.write_text(json.dumps(settings, sort_keys=True, indent=2), encoding="utf-8")
    path.chmod(0o600)
    return path


def wrap_argv(srt_path: Path, settings_path: Path, argv: Sequence[str]) -> list[str]:
    """Return the argv that runs ``argv`` through ``srt``."""
    return [str(srt_path), "--settings", str(settings_path), "--", *argv]


def find_srt(settings: Settings) -> Path:
    """Locate the pinned ``srt`` binary.

    An explicit :attr:`Settings.srt_path` wins. Otherwise the copy installed by
    ``make sandbox-install`` under ``data_dir`` is used. There is deliberately
    no ``PATH`` fallback: a ``PATH`` lookup could pick up an unpinned copy.
    """
    if settings.srt_path is not None:
        candidate = Path(settings.srt_path).expanduser()
        if not _is_executable_file(candidate):
            raise SandboxUnavailable(f"srt_path {str(candidate)!r} is not an executable file")
        return candidate

    candidate = Path(settings.data_dir).expanduser() / "tools" / "node_modules" / ".bin" / "srt"
    if not _is_executable_file(candidate):
        raise SandboxUnavailable(
            f"sandbox-runtime (srt) not found at {str(candidate)!r}; run 'make sandbox-install'"
        )
    return candidate


def _absolute(path: Path) -> Path:
    """Expand ``~`` and resolve symlinks; never emit a ``~`` path."""
    return Path(path).expanduser().resolve()


def _absolute_paths(paths: Sequence[Path]) -> tuple[Path, ...]:
    return tuple(_absolute(path) for path in paths)


def _existing_absolute_paths(paths: Sequence[Path]) -> tuple[Path, ...]:
    """Keep only toolchain paths that exist, in order, without duplicates."""
    result: list[Path] = []
    seen: set[Path] = set()
    for path in paths:
        absolute = _absolute(path)
        if absolute.exists() and absolute not in seen:
            result.append(absolute)
            seen.add(absolute)
    return tuple(result)


def _dedupe_paths(paths: Sequence[Path]) -> tuple[Path, ...]:
    result: list[Path] = []
    seen: set[Path] = set()
    for path in paths:
        if path not in seen:
            result.append(path)
            seen.add(path)
    return tuple(result)


def _sensitive_paths(home: Path, data_dir: Path | None = None) -> tuple[Path, ...]:
    paths = _absolute_paths(
        (
            *[home / relative for relative in _SENSITIVE_RELATIVE],
            *[home / relative for relative in _SHELL_PROFILE_RELATIVE],
        )
    )
    return paths if data_dir is None else (*paths, data_dir)


def _validate_data_dir_entries(entries: Sequence[Path], data_dir: Path) -> None:
    """Allow one worktree (or tmp dir) inside ``data_dir``, never the whole tree.

    ``_validate_allow_entry`` already refuses ``data_dir`` and its ancestors,
    because ``data_dir`` is in the sensitive list. This adds the generalised
    form of the ``~/.gaia/worktrees`` rule for a data dir anywhere on disk.
    """
    for entry in entries:
        if entry == data_dir / "worktrees":
            raise SandboxConfigError(
                f"sandbox may not allow access to {str(entry)!r} itself; allow one worktree"
            )


def _validate_allow_entries(
    allow_read: Sequence[Path],
    allow_write: Sequence[Path],
    home: Path,
    sensitive_paths: Sequence[Path],
) -> None:
    """Refuse an allow entry that would open the whole home or a sensitive path."""
    for entry in (*allow_read, *allow_write):
        _validate_allow_entry(entry, home, sensitive_paths)


def _validate_allow_entry(entry: Path, home: Path, sensitive_paths: Sequence[Path]) -> None:
    if entry == home:
        raise SandboxConfigError("sandbox may not allow access to the whole home directory")
    if entry.parent == entry:  # the filesystem root
        raise SandboxConfigError("sandbox may not allow access to '/'")
    if home.is_relative_to(entry):
        raise SandboxConfigError(
            f"sandbox may not allow access to an ancestor of home: {str(entry)!r}"
        )
    if entry == _absolute(home / ".gaia" / "worktrees"):
        raise SandboxConfigError(
            "sandbox may not allow access to ~/.gaia/worktrees itself; allow one worktree"
        )
    for sensitive in sensitive_paths:
        if entry == sensitive or sensitive.is_relative_to(entry):
            raise SandboxConfigError(
                f"sandbox may not allow access to sensitive path {str(sensitive)!r}"
            )


def _deny_write_entries(worktree: Path) -> list[Any]:
    """Writes that stay denied even though the worktree is allowWrite."""
    return [
        {"path": str(worktree / ".git"), "literal": True},
        str(worktree / ".github" / "workflows"),
        str(worktree / ".gaia"),
        str(worktree / ".claude"),
        str(worktree / ".cline"),
        f"{worktree}/**/.envrc",
        f"{worktree}/**/.env",
        f"{worktree}/**/.env.*",
    ]


def _network_domains(allowed_domains: Sequence[str], gateway: str | None) -> list[str]:
    domains = list(allowed_domains)
    if gateway is not None:
        domains.append(gateway)
    for domain in domains:
        _validate_domain(domain)
    return domains


def _validate_domain(domain: str) -> None:
    """Validate one allowedDomains entry (no scheme, path, whitespace or bare ``*``)."""
    if not domain:
        raise SandboxConfigError("network allowedDomains entries must not be empty")
    if domain == "*":
        raise SandboxConfigError("network allowedDomains entry '*' is forbidden")
    if "://" in domain:
        raise SandboxConfigError(f"network domain {domain!r} must not include a scheme")
    if "/" in domain or "\\" in domain:
        raise SandboxConfigError(f"network domain {domain!r} must not include a path")
    if any(character.isspace() for character in domain):
        raise SandboxConfigError(f"network domain {domain!r} must not contain whitespace")


def _is_executable_file(path: Path) -> bool:
    """True when ``path`` is a regular file and executable by the current user."""
    return path.is_file() and os.access(path, os.X_OK)
