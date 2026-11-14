"""Doc 09a task 6: the srt sandbox profile builder and binary locator."""

from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest

from gaia.config.settings import Settings
from gaia.security.sandbox import (
    SandboxConfigError,
    SandboxSpec,
    SandboxUnavailable,
    build_srt_settings,
    find_srt,
    wrap_argv,
    write_srt_settings,
)


@pytest.fixture
def home(tmp_path: Path) -> Path:
    path = tmp_path / "home"
    path.mkdir()
    (path / ".ssh").mkdir()
    (path / ".ssh" / "id_test").write_text("secret", encoding="utf-8")
    return path


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


def spec(
    worktree: Path,
    tmp_dir: Path,
    home: Path,
    *,
    allowed_domains: tuple[str, ...] = (),
    gateway: str | None = None,
    toolchain_read: tuple[Path, ...] = (),
    extra_write: tuple[Path, ...] = (),
) -> SandboxSpec:
    return SandboxSpec(
        worktree=worktree,
        tmp_dir=tmp_dir,
        home=home,
        allowed_domains=allowed_domains,
        gateway=gateway,
        toolchain_read=toolchain_read,
        extra_write=extra_write,
    )


def test_build_srt_settings_structure(home: Path, worktree: Path, tmp_dir: Path) -> None:
    settings = build_srt_settings(spec(worktree, tmp_dir, home))

    assert settings["network"] == {
        "allowedDomains": [],
        "deniedDomains": [],
        "allowLocalBinding": False,
        "allowUnixSockets": [],
    }
    assert settings["filesystem"]["allowRead"] == [
        str(worktree.resolve()),
        str(tmp_dir.resolve()),
    ]
    assert settings["filesystem"]["allowWrite"] == [
        str(worktree.resolve()),
        str(tmp_dir.resolve()),
    ]
    assert str(home.resolve()) in settings["filesystem"]["denyRead"]
    assert settings["allowPty"] is False
    assert settings["allowAppleEvents"] is False
    assert settings["enableWeakerNestedSandbox"] is False
    assert settings["enableWeakerNetworkIsolation"] is False


def test_sensitive_paths_are_denied_explicitly(home: Path, worktree: Path, tmp_dir: Path) -> None:
    settings = build_srt_settings(spec(worktree, tmp_dir, home))

    deny_read = settings["filesystem"]["denyRead"]
    for relative in (
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
        ".bashrc",
        ".zshrc",
        ".config/fish/config.fish",
    ):
        assert str((home / relative).resolve()) in deny_read


def test_toolchain_read_keeps_only_existing_paths(
    home: Path, worktree: Path, tmp_dir: Path
) -> None:
    existing = tmp_dir.parent / "toolchain"
    existing.mkdir()
    missing = tmp_dir.parent / "missing"

    settings = build_srt_settings(spec(worktree, tmp_dir, home, toolchain_read=(existing, missing)))

    allow_read = settings["filesystem"]["allowRead"]
    assert str(existing.resolve()) in allow_read
    assert str(missing.resolve()) not in allow_read


def test_extra_write_is_added_to_allow_write(home: Path, worktree: Path, tmp_dir: Path) -> None:
    extra = tmp_dir.parent / "extra"
    settings = build_srt_settings(spec(worktree, tmp_dir, home, extra_write=(extra,)))

    assert str(extra.resolve()) in settings["filesystem"]["allowWrite"]


def test_duplicate_allow_write_entries_are_deduplicated(
    home: Path, worktree: Path, tmp_dir: Path
) -> None:
    settings = build_srt_settings(spec(worktree, tmp_dir, home, extra_write=(worktree,)))

    assert settings["filesystem"]["allowWrite"] == [
        str(worktree.resolve()),
        str(tmp_dir.resolve()),
    ]


def test_deny_write_entries(home: Path, worktree: Path, tmp_dir: Path) -> None:
    settings = build_srt_settings(spec(worktree, tmp_dir, home))

    deny_write = settings["filesystem"]["denyWrite"]
    worktree_abs = worktree.resolve()
    assert {"path": str(worktree_abs / ".git"), "literal": True} in deny_write
    assert str(worktree_abs / ".github" / "workflows") in deny_write
    assert str(worktree_abs / ".gaia") in deny_write
    assert str(worktree_abs / ".claude") in deny_write
    assert str(worktree_abs / ".cline") in deny_write
    assert f"{worktree_abs}/**/.env" in deny_write
    assert f"{worktree_abs}/**/.env.*" in deny_write


def test_network_allows_repo_allowlist_and_gateway(
    home: Path, worktree: Path, tmp_dir: Path
) -> None:
    settings = build_srt_settings(
        spec(
            worktree,
            tmp_dir,
            home,
            allowed_domains=("example.com", "*.example.com"),
            gateway="127.0.0.1:8765",
        )
    )

    assert settings["network"]["allowedDomains"] == [
        "example.com",
        "*.example.com",
        "127.0.0.1:8765",
    ]
    assert settings["network"]["deniedDomains"] == []
    assert settings["network"]["allowLocalBinding"] is False
    assert settings["network"]["allowUnixSockets"] == []


@pytest.mark.parametrize(
    "domain",
    ["", "*", "https://example.com", "example.com/path", "example.com\\path", "bad domain"],
)
def test_invalid_network_domains_fail_closed(
    home: Path, worktree: Path, tmp_dir: Path, domain: str
) -> None:
    with pytest.raises(SandboxConfigError):
        build_srt_settings(spec(worktree, tmp_dir, home, allowed_domains=(domain,)))


def test_invalid_gateway_fails_closed(home: Path, worktree: Path, tmp_dir: Path) -> None:
    with pytest.raises(SandboxConfigError):
        build_srt_settings(spec(worktree, tmp_dir, home, gateway="https://example.com"))


@pytest.mark.parametrize(
    "kind",
    ["worktree", "tmp_dir", "toolchain", "extra_write"],
)
def test_allow_entries_may_not_be_home(
    home: Path, worktree: Path, tmp_dir: Path, kind: str
) -> None:
    if kind == "worktree":
        built = SandboxSpec(worktree=home, tmp_dir=tmp_dir, home=home)
    elif kind == "tmp_dir":
        built = SandboxSpec(worktree=worktree, tmp_dir=home, home=home)
    elif kind == "toolchain":
        built = SandboxSpec(worktree=worktree, tmp_dir=tmp_dir, home=home, toolchain_read=(home,))
    else:
        built = SandboxSpec(worktree=worktree, tmp_dir=tmp_dir, home=home, extra_write=(home,))

    with pytest.raises(SandboxConfigError, match="whole home"):
        build_srt_settings(built)


def test_allow_entries_may_not_be_an_ancestor_of_home(
    home: Path, worktree: Path, tmp_dir: Path
) -> None:
    with pytest.raises(SandboxConfigError, match="ancestor of home"):
        build_srt_settings(spec(home.parent, tmp_dir, home))


def test_allow_entries_may_not_be_the_filesystem_root(
    home: Path, worktree: Path, tmp_dir: Path
) -> None:
    with pytest.raises(SandboxConfigError, match="access to '/'"):
        build_srt_settings(spec(Path("/"), tmp_dir, home))


@pytest.mark.parametrize(
    "relative",
    [".ssh", ".aws", ".config", ".gnupg", "Library/Keychains", ".gaia"],
)
def test_allow_entries_may_not_be_a_sensitive_path(
    home: Path, worktree: Path, tmp_dir: Path, relative: str
) -> None:
    sensitive = home / relative

    with pytest.raises(SandboxConfigError, match="sensitive path"):
        build_srt_settings(spec(sensitive, tmp_dir, home))


def test_worktree_may_live_under_gaia_worktrees_but_not_the_parent(
    home: Path, worktree: Path, tmp_dir: Path
) -> None:
    worktree_root = home / ".gaia" / "worktrees"
    worktree_root.mkdir(parents=True)
    (worktree_root / "repo").mkdir()

    allowed = build_srt_settings(spec(worktree_root / "repo", tmp_dir, home))
    assert str((worktree_root / "repo").resolve()) in allowed["filesystem"]["allowRead"]

    with pytest.raises(SandboxConfigError, match="allow one worktree"):
        build_srt_settings(spec(worktree_root, tmp_dir, home))


def test_write_srt_settings_writes_owner_only_json(
    home: Path, worktree: Path, tmp_dir: Path
) -> None:
    directory = tmp_dir.parent / "settings"
    built = spec(worktree, tmp_dir, home)

    path = write_srt_settings(built, directory)

    assert path.name == "srt-settings.json"
    assert json.loads(path.read_text(encoding="utf-8")) == build_srt_settings(built)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_write_srt_settings_rejects_a_file_as_the_directory(
    home: Path, worktree: Path, tmp_dir: Path
) -> None:
    directory = tmp_dir.parent / "settings"
    directory.write_text("not a directory", encoding="utf-8")

    with pytest.raises(SandboxConfigError, match="not a directory"):
        write_srt_settings(spec(worktree, tmp_dir, home), directory)


def test_wrap_argv() -> None:
    argv = wrap_argv(
        Path("/opt/srt"),
        Path("/tmp/settings.json"),
        ["pytest", "-q", "tests"],
    )

    assert argv == [
        "/opt/srt",
        "--settings",
        "/tmp/settings.json",
        "--",
        "pytest",
        "-q",
        "tests",
    ]


def test_find_srt_prefers_the_explicit_settings_path(tmp_path: Path) -> None:
    srt = tmp_path / "srt"
    srt.write_text("#!/bin/sh\n", encoding="utf-8")
    srt.chmod(0o755)
    settings = Settings(data_dir=tmp_path / "data", srt_path=srt)

    assert find_srt(settings) == srt


def test_find_srt_rejects_a_non_executable_explicit_path(tmp_path: Path) -> None:
    srt = tmp_path / "srt"
    srt.write_text("#!/bin/sh\n", encoding="utf-8")
    settings = Settings(data_dir=tmp_path / "data", srt_path=srt)

    with pytest.raises(SandboxUnavailable, match="not an executable file"):
        find_srt(settings)


def test_find_srt_rejects_a_missing_explicit_path(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path / "data", srt_path=tmp_path / "missing")

    with pytest.raises(SandboxUnavailable, match="not an executable file"):
        find_srt(settings)


def test_find_srt_uses_the_pinned_data_dir_copy(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    srt = data_dir / "tools" / "node_modules" / ".bin" / "srt"
    srt.parent.mkdir(parents=True)
    srt.write_text("#!/bin/sh\n", encoding="utf-8")
    srt.chmod(0o755)

    assert find_srt(Settings(data_dir=data_dir)) == srt


def test_find_srt_does_not_fall_back_to_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    srt = bin_dir / "srt"
    srt.write_text("#!/bin/sh\n", encoding="utf-8")
    srt.chmod(0o755)
    monkeypatch.setenv("PATH", str(bin_dir))

    with pytest.raises(SandboxUnavailable, match="make sandbox-install"):
        find_srt(Settings(data_dir=tmp_path / "data"))


def test_find_srt_missing_copy_mentions_install_target(tmp_path: Path) -> None:
    with pytest.raises(SandboxUnavailable, match="make sandbox-install"):
        find_srt(Settings(data_dir=tmp_path / "data"))


def test_data_dir_outside_home_is_denied_and_one_worktree_inside_it_allowed(
    home: Path, tmp_path: Path, tmp_dir: Path
) -> None:
    """Lead fix (review 09a, S5): GAIA_DATA_DIR need not live under home."""
    data_dir = tmp_path / "elsewhere" / "gaia-data"
    (data_dir / "worktrees" / "repo").mkdir(parents=True)
    worktree = data_dir / "worktrees" / "repo"

    settings = build_srt_settings(
        SandboxSpec(worktree=worktree, tmp_dir=tmp_dir, home=home, data_dir=data_dir)
    )

    assert str(data_dir.resolve()) in settings["filesystem"]["denyRead"]
    assert str(worktree.resolve()) in settings["filesystem"]["allowRead"]


@pytest.mark.parametrize("relative", ["", "worktrees"])
def test_allow_entries_may_not_open_the_data_dir(
    home: Path, tmp_path: Path, tmp_dir: Path, relative: str
) -> None:
    data_dir = tmp_path / "gaia-data"
    (data_dir / "worktrees").mkdir(parents=True)

    with pytest.raises(SandboxConfigError):
        build_srt_settings(
            SandboxSpec(
                worktree=data_dir / relative if relative else data_dir,
                tmp_dir=tmp_dir,
                home=home,
                data_dir=data_dir,
            )
        )


def test_envrc_is_write_denied(home: Path, worktree: Path, tmp_dir: Path) -> None:
    settings = build_srt_settings(spec(worktree, tmp_dir, home))

    assert f"{worktree.resolve()}/**/.envrc" in settings["filesystem"]["denyWrite"]
