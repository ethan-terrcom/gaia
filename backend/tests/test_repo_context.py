"""Repository context builder (doc 11)."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from gaia.domain.plans import TicketInput
from gaia.orchestrator import repo_context as repo_context_module
from gaia.orchestrator.repo_context import RepoContext, build_repo_context


def _ticket(body: str) -> TicketInput:
    return TicketInput(id="t1", title="Fix the thing", body=body)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / "src").mkdir(parents=True)
    (root / "src" / "nested").mkdir()
    (root / "tests").mkdir()
    (root / ".git").mkdir()
    (root / ".venv").mkdir()
    (root / "node_modules").mkdir()
    (root / "__pycache__").mkdir()
    (root / "dist").mkdir()
    (root / "build").mkdir()
    (root / "README.md").write_text("hello readme", encoding="utf-8")
    (root / "src" / "app.py").write_text("print('app')", encoding="utf-8")
    (root / "src" / "nested" / "deep.py").write_text("x = 1", encoding="utf-8")
    (root / "tests" / "test_app.py").write_text("def test_app(): ...", encoding="utf-8")
    (root / "notes.txt").write_text("notes", encoding="utf-8")
    return root


async def test_build_repo_context_includes_readme_tree_and_named_files(repo: Path) -> None:
    context = await build_repo_context(
        repo,
        _ticket("Please change src/app.py and tests/test_app.py."),
    )

    assert context.readme == "hello readme"
    assert "src/" in context.tree
    assert "README.md" in context.tree
    assert ".git" not in context.tree
    assert ".venv" not in context.tree
    assert "node_modules" not in context.tree
    assert "__pycache__" not in context.tree
    assert "dist" not in context.tree
    assert "build" not in context.tree
    assert dict(context.files)["src/app.py"] == "print('app')"
    assert dict(context.files)["tests/test_app.py"] == "def test_app(): ..."


async def test_build_repo_context_ignores_denied_and_non_file_paths(repo: Path) -> None:
    context = await build_repo_context(
        repo,
        _ticket("Look at ../secret.py, .git/config, src and src/nested."),
    )

    assert "secret.py" not in dict(context.files)
    assert ".git/config" not in dict(context.files)
    assert "src" not in dict(context.files)
    assert "src/nested" not in dict(context.files)


async def test_build_repo_context_caps_readme_and_files(repo: Path) -> None:
    (repo / "README.md").write_text("r" * 40_001, encoding="utf-8")
    (repo / "big.py").write_text("b" * 40_001, encoding="utf-8")

    context = await build_repo_context(repo, _ticket("Change big.py."))

    assert context.readme is not None
    assert len(context.readme) == 40_000
    assert len(dict(context.files)["big.py"]) == 40_000
    assert any("README truncated" in note for note in context.omitted)
    assert any("file big.py truncated" in note for note in context.omitted)


async def test_build_repo_context_stops_at_budget(repo: Path) -> None:
    (repo / "a.py").write_text("a" * 20, encoding="utf-8")
    (repo / "b.py").write_text("b" * 20, encoding="utf-8")
    (repo / "c.py").write_text("c" * 20, encoding="utf-8")

    context = await build_repo_context(
        repo,
        _ticket("Change a.py, b.py and c.py."),
        budget_chars=125,
    )

    assert "a.py" in dict(context.files)
    assert "b.py" not in dict(context.files)
    assert any("b.py omitted (context budget)" in note for note in context.omitted)
    assert any("ticket file contents omitted" in note for note in context.omitted)


async def test_repo_context_render_has_sections(repo: Path) -> None:
    context = RepoContext(
        readme="readme",
        tree="src/",
        files=(("src/app.py", "print('app')"),),
        omitted=("file x omitted (context budget)",),
    )

    rendered = context.render()

    assert "README" in rendered
    assert "Repository tree (depth 2)" in rendered
    assert "File: src/app.py" in rendered
    assert "Omitted" in rendered
    assert "file x omitted (context budget)" in rendered


async def test_repo_context_render_with_empty_optional_parts() -> None:
    context = RepoContext(readme=None, tree="", files=(), omitted=())

    assert context.render() == ""


async def test_build_repo_context_without_readme(tmp_path: Path) -> None:
    root = tmp_path / "empty"
    root.mkdir()

    context = await build_repo_context(root, _ticket(""))

    assert context.readme is None
    assert context.tree == ""


async def test_build_repo_context_with_missing_root(tmp_path: Path) -> None:
    context = await build_repo_context(tmp_path / "missing", _ticket(""))

    assert context.readme is None
    assert context.tree == ""


async def test_build_repo_context_readme_directory_is_skipped(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    (root / "README").mkdir()

    context = await build_repo_context(root, _ticket(""))

    assert context.readme is None


async def test_build_repo_context_readme_read_error_is_skipped(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake_read_text(path: Path, *args: object, **kwargs: object) -> str:
        raise OSError("boom")

    monkeypatch.setattr(Path, "read_text", fake_read_text)

    context = await build_repo_context(repo, _ticket(""))

    assert context.readme is None


async def test_build_repo_context_skips_symlinks(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    (root / "real.py").write_text("x", encoding="utf-8")
    os.symlink(root / "real.py", root / "link.py")

    context = await build_repo_context(root, _ticket(""))

    assert "link.py" not in context.tree
    assert "real.py" in context.tree


async def test_build_repo_context_skips_entries_that_stat_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    (root / "a.py").write_text("x", encoding="utf-8")

    def fake_is_symlink(self: object) -> bool:
        raise OSError("boom")

    monkeypatch.setattr(os.DirEntry, "is_symlink", fake_is_symlink)

    context = await build_repo_context(root, _ticket(""))

    assert context.tree == ""


async def test_build_repo_context_skips_non_regular_files(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    (root / "pipe.py")
    os.mkfifo(root / "pipe.py")

    context = await build_repo_context(root, _ticket(""))

    assert "pipe.py" not in context.tree


async def test_build_repo_context_truncates_tree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "repo"
    (root / "adir").mkdir(parents=True)
    (root / "adir" / "child1.py").write_text("1", encoding="utf-8")
    (root / "adir" / "child2.py").write_text("2", encoding="utf-8")
    (root / "z.py").write_text("z", encoding="utf-8")
    monkeypatch.setattr(repo_context_module, "TREE_ENTRY_CAP", 2)

    context = await build_repo_context(root, _ticket(""))

    assert "child1.py" in context.tree
    assert "child2.py" not in context.tree
    assert "z.py" not in context.tree
    assert any("repository tree truncated at 2 entries" in note for note in context.omitted)


async def test_build_repo_context_deduplicates_path_candidates(repo: Path) -> None:
    context = await build_repo_context(repo, _ticket("Change src/app.py and src/app.py."))

    assert len(dict(context.files)) == 1


async def test_build_repo_context_skips_directory_with_extension_name(
    tmp_path: Path,
) -> None:
    root = tmp_path / "repo"
    (root / "pkg.py").mkdir(parents=True)

    context = await build_repo_context(root, _ticket("Change pkg.py."))

    assert "pkg.py" not in dict(context.files)


async def test_build_repo_context_skips_unreadable_ticket_file(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original_read_capped = repo_context_module._read_capped

    def fake_read_capped(path: Path, cap: int) -> tuple[str, bool]:
        if path.name == "app.py":
            raise OSError("boom")
        return original_read_capped(path, cap)

    monkeypatch.setattr(repo_context_module, "_read_capped", fake_read_capped)

    context = await build_repo_context(repo, _ticket("Change src/app.py."))

    assert "src/app.py" not in dict(context.files)


async def test_build_repo_context_readme_budget_omits_everything(
    repo: Path,
) -> None:
    context = await build_repo_context(repo, _ticket(""), budget_chars=1)

    assert context.readme is None
    assert context.files == ()
    assert "README omitted (context budget)" in context.omitted
    assert "repository tree omitted (context budget)" in context.omitted
    assert "ticket file contents omitted (context budget)" in context.omitted


async def test_build_repo_context_tree_budget_omits_tree(
    repo: Path,
) -> None:
    context = await build_repo_context(repo, _ticket(""), budget_chars=13)

    assert context.readme == "hello readme"
    assert context.tree == ""
    assert "repository tree omitted (context budget)" in context.omitted
    assert "ticket file contents omitted (context budget)" in context.omitted


async def test_build_repo_context_last_file_budget_omission_has_no_tail_note(
    repo: Path,
) -> None:
    (repo / "a.py").write_text("a" * 20, encoding="utf-8")

    context = await build_repo_context(repo, _ticket("Change a.py."), budget_chars=100)

    assert "file a.py omitted (context budget)" in context.omitted
    assert not any("ticket file contents omitted" in note for note in context.omitted)
