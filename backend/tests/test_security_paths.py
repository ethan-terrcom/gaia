"""Doc 09a task 1 and task 5: path confinement and write classification."""

from __future__ import annotations

from pathlib import Path

import pytest

from gaia.security.paths import PathDenied, UnsafePath, classify_write, confine, glob_match


@pytest.fixture
def root(tmp_path: Path) -> Path:
    path = tmp_path / "root"
    path.mkdir()
    return path


@pytest.mark.parametrize(
    ("user_path", "reason"),
    [
        ("", "path is empty"),
        ("foo\0bar", "NUL byte"),
        ("../x", "resolves outside the root"),
        ("/etc/passwd", "absolute, home-relative or Windows"),
        ("~/x", "absolute, home-relative or Windows"),
        ("~root", "absolute, home-relative or Windows"),
        ("C:\\x", "absolute, home-relative or Windows"),
        ("\\\\server\\share", "absolute, home-relative or Windows"),
        (".git/config", "the .git directory is reserved"),
        (".GIT/config", "the .git directory is reserved"),
        ("a/../.git", "the .git directory is reserved"),
    ],
)
def test_confine_refuses_escapes(root: Path, user_path: str, reason: str) -> None:
    with pytest.raises(PathDenied, match=reason):
        confine(root, user_path)


@pytest.mark.parametrize(
    ("user_path", "tail"),
    [
        ("file.txt", ("file.txt",)),
        ("sub/.", ("sub",)),
        ("a/../b", ("b",)),
        (".gít/config", (".gít", "config")),
    ],
)
def test_confine_returns_a_path_inside_the_root(
    root: Path, user_path: str, tail: tuple[str, ...]
) -> None:
    result = confine(root, user_path)

    assert result.is_relative_to(root.resolve())
    assert result == root.resolve().joinpath(*tail)


def test_confine_refuses_a_symlink_to_outside(root: Path, tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret").write_text("secret", encoding="utf-8")
    (root / "link").symlink_to(outside)

    with pytest.raises(PathDenied, match="symlink outside the root"):
        confine(root, "link/secret")


def test_confine_refuses_a_symlinked_parent_directory(root: Path, tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "parent-link").symlink_to(outside)

    with pytest.raises(PathDenied, match="symlink outside the root"):
        confine(root, "parent-link/new.txt")


def test_confine_refuses_a_dangling_symlink_pointing_outside(root: Path, tmp_path: Path) -> None:
    (root / "dangling").symlink_to(tmp_path / "outside" / "missing")

    with pytest.raises(PathDenied):
        confine(root, "dangling/file")


def test_confine_allows_a_symlink_that_stays_inside(root: Path) -> None:
    (root / "inside").mkdir()
    (root / "inside-link").symlink_to(root / "inside")

    result = confine(root, "inside-link/file")

    assert result == root.resolve() / "inside" / "file"


@pytest.mark.parametrize("target", [".git", ".git/config", ".GIT"])
def test_confine_refuses_an_inside_symlink_that_resolves_into_git(root: Path, target: str) -> None:
    """Lead fix (review 09a, S1): a harmless spelling must not resolve into .git."""
    (root / ".git").mkdir()
    (root / ".git" / "config").write_text("[core]\n")
    (root / "alias").symlink_to(root / target)

    with pytest.raises(PathDenied, match=r"resolves into the \.git directory"):
        confine(root, "alias")


def test_confine_refuses_a_path_through_a_symlinked_parent_into_git(root: Path) -> None:
    (root / ".git" / "hooks").mkdir(parents=True)
    (root / "tools").symlink_to(root / ".git")

    with pytest.raises(PathDenied, match=r"resolves into the \.git directory"):
        confine(root, "tools/hooks/pre-commit")


def test_confine_refuses_a_missing_root(tmp_path: Path) -> None:
    with pytest.raises(PathDenied, match="not an existing directory"):
        confine(tmp_path / "missing", "file.txt")


def test_confine_refuses_a_non_string_path(root: Path) -> None:
    with pytest.raises(TypeError):
        confine(root, b"file.txt")  # type: ignore[arg-type]


def test_confine_never_echoes_more_than_200_chars_of_input(root: Path) -> None:
    long_input = "/" + ("a" * 250)

    with pytest.raises(PathDenied) as excinfo:
        confine(root, long_input)

    assert "a" * 201 not in str(excinfo.value)


def test_unsafe_path_is_importable_from_git() -> None:
    from gaia.services.git import UnsafePath as GitUnsafePath

    assert GitUnsafePath is UnsafePath


@pytest.mark.parametrize(
    ("relative", "reason"),
    [
        (".github/workflows/deploy.yml", ".github/workflows/**"),
        ("foo.lock", "**/*.lock"),
        ("sub/foo.lock", "**/*.lock"),
        ("package-lock.json", "package-lock.json"),
        ("sub/package-lock.json", "package-lock.json"),
        ("pnpm-lock.yaml", "pnpm-lock.yaml"),
        ("sub/pnpm-lock.yaml", "pnpm-lock.yaml"),
        ("yarn.lock", "yarn.lock"),
        ("sub/yarn.lock", "yarn.lock"),
        ("uv.lock", "uv.lock"),
        ("sub/uv.lock", "uv.lock"),
        ("poetry.lock", "poetry.lock"),
        ("sub/poetry.lock", "poetry.lock"),
        (".env", ".env at any depth"),
        ("sub/.env", ".env at any depth"),
        (".env.production", ".env.* at any depth"),
        ("sub/.env.production", ".env.* at any depth"),
        (".envrc", ".envrc at any depth"),
        ("sub/.envrc", ".envrc at any depth"),
        (".pre-commit-config.yaml", ".pre-commit-config.yaml"),
        (".husky/pre-commit", ".husky/**"),
        (".gaia/config.toml", ".gaia/**"),
        (".claude/settings.json", ".claude/**"),
        (".cline/hooks/pre_tool_use", ".cline/**"),
        (".clinerules", ".clinerules"),
        (".mcp.json", ".mcp.json"),
        (".vscode/settings.json", ".vscode/**"),
        (".idea/workspace.xml", ".idea/**"),
    ],
)
def test_classify_write_asks_for_protected_paths(root: Path, relative: str, reason: str) -> None:
    decision = classify_write(root, root / relative)

    assert decision.action == "ask"
    assert reason in decision.reason


@pytest.mark.parametrize(
    "relative",
    [
        "src/main.py",
        "README.md",
        ".github/other.yml",
        "sub/.github/workflows/deploy.yml",
        ".gít/config",
        ".gitignore",
        "package.json",
    ],
)
def test_classify_write_allows_ordinary_paths(root: Path, relative: str) -> None:
    decision = classify_write(root, root / relative)

    assert decision.action == "allow"
    assert "allow: write to" in decision.reason


def test_classify_write_matches_case_insensitively(root: Path) -> None:
    decision = classify_write(root, root / "PACKAGE-LOCK.JSON")

    assert decision.action == "ask"
    assert "package-lock.json" in decision.reason


def test_classify_write_allows_the_root_itself(root: Path) -> None:
    decision = classify_write(root, root)

    assert decision.action == "allow"
    assert "worktree root" in decision.reason


def test_classify_write_refuses_a_path_outside_the_root(root: Path, tmp_path: Path) -> None:
    with pytest.raises(PathDenied, match="outside root"):
        classify_write(root, tmp_path / "other")


def test_glob_match_empty_pattern() -> None:
    assert glob_match("", "") is True
    assert glob_match("a", "") is False
