"""Tests for the git tools (doc 09b)."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import cast

import pytest

from gaia.runtimes.base import AgentTask
from gaia.services.git import GitAuthor, GitError, GitService
from gaia.tools import git_tools
from gaia.tools.registry import ToolContext, ToolRegistry
from tests.git_repo import git
from tests.runtime_contract import base_sha, make_worktree_location
from tests.tool_fakes import make_proc_result, make_tool_context


@pytest.fixture
def registry() -> ToolRegistry:
    built = ToolRegistry()
    built.register(git_tools.git_status)
    built.register(git_tools.git_diff)
    built.register(git_tools.git_commit)
    return built


@pytest.fixture
def ctx(git_repo: Path, git_service: GitService, tmp_path: Path) -> ToolContext:
    location = make_worktree_location(git_repo, tmp_path)
    base = base_sha(git_repo)
    base_ctx = make_tool_context(location.work_tree, git=git_service)
    task = AgentTask(
        task_id="task-git",
        run_id="run-git",
        run_step_id=None,
        workspace_id="ws-tools",
        agent_id=None,
        role=base_ctx.task.role,
        instructions="git tools",
        worktree=location,
        base_sha=base,
    )
    return replace(base_ctx, task=task)


async def test_git_status_clean(registry: ToolRegistry, ctx: ToolContext) -> None:
    result = await registry.call("git_status", {}, ctx, call_id="call-status-clean")

    assert result.ok is True
    assert result.content == "clean"


async def test_git_status_dirty(registry: ToolRegistry, ctx: ToolContext) -> None:
    (ctx.root / "new.txt").write_text("new\n", encoding="utf-8")

    result = await registry.call("git_status", {}, ctx, call_id="call-status-dirty")

    assert result.ok is True
    assert "new.txt" in result.content


async def test_git_diff_confines_the_path(registry: ToolRegistry, ctx: ToolContext) -> None:
    readme = ctx.root / "README.md"
    readme.write_text(readme.read_text(encoding="utf-8") + "hello\n", encoding="utf-8")

    result = await registry.call("git_diff", {"path": "README.md"}, ctx, call_id="call-diff-path")

    assert result.ok is True
    assert "README.md" in result.content


async def test_git_diff_path_escape_is_refused(registry: ToolRegistry, ctx: ToolContext) -> None:
    result = await registry.call("git_diff", {"path": "../secret"}, ctx, call_id="call-diff-escape")

    assert result.ok is False
    assert result.content.startswith("path denied: ")
    assert result.decision == "deny"


async def test_git_commit_uses_the_agent_author(registry: ToolRegistry, ctx: ToolContext) -> None:
    (ctx.root / "work.txt").write_text("work\n", encoding="utf-8")

    result = await registry.call("git_commit", {"message": "Add work"}, ctx, call_id="call-commit")

    assert result.ok is True
    assert result.content
    assert git(ctx.root, "log", "-1", "--format=%an <%ae>").strip() == (
        "Gaia (junior-1) <gaia@localhost>"
    )


async def test_git_commit_nothing_to_commit(registry: ToolRegistry, ctx: ToolContext) -> None:
    result = await registry.call(
        "git_commit", {"message": "nothing here"}, ctx, call_id="call-commit-empty"
    )

    assert result.ok is False
    assert result.content == "nothing to commit"


@pytest.mark.parametrize(
    ("name", "email"),
    [
        ("bad\nname", "gaia@localhost"),
        ("bad<name>", "gaia@localhost"),
        ("bad\x00name", "gaia@localhost"),
        ("a" * 101, "gaia@localhost"),
        ("Gaia", "bad\nemail"),
        ("Gaia", "bad<email>"),
        ("Gaia", "a" * 101),
    ],
)
def test_git_author_refuses_invalid_values(name: str, email: str) -> None:
    with pytest.raises(ValueError, match="GitAuthor"):
        GitAuthor(name=name, email=email)


def test_git_author_accepts_a_valid_identity() -> None:
    author = GitAuthor(name="Gaia (junior-1)", email="gaia@localhost")

    assert author.name == "Gaia (junior-1)"
    assert author.email == "gaia@localhost"


def test_git_author_refuses_a_non_string_field() -> None:
    with pytest.raises(ValueError, match="must be a str"):
        GitAuthor(name=cast(str, 123), email="gaia@localhost")


async def test_git_commit_rejects_nul_and_overlong_first_line(
    registry: ToolRegistry, ctx: ToolContext
) -> None:
    nul_result = await registry.call(
        "git_commit",
        {"message": "bad\x00message"},
        ctx,
        call_id="call-commit-nul",
    )
    assert nul_result.ok is False
    assert "NUL" in nul_result.content

    long_result = await registry.call(
        "git_commit",
        {"message": "a" * 201},
        ctx,
        call_id="call-commit-long-first-line",
    )
    assert long_result.ok is False
    assert "first line" in long_result.content


async def test_git_diff_without_path_and_staged(registry: ToolRegistry, ctx: ToolContext) -> None:
    result = await registry.call("git_diff", {"staged": True}, ctx, call_id="call-diff-staged")

    assert result.ok is True
    assert result.content == ""


async def test_git_diff_reports_a_git_failure(
    ctx: ToolContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_invoke(*args, cwd, env, timeout_s=None, max_output_bytes):
        del args, cwd, env, max_output_bytes
        return make_proc_result(["git"], cwd=ctx.root, returncode=1, stderr="diff broken")

    monkeypatch.setattr(ctx.git, "_invoke", fake_invoke)

    result = await git_tools._git_diff_handler(git_tools.GitDiffInput(), ctx)

    assert result.ok is False
    assert result.content == "diff broken"


async def test_git_diff_notes_a_truncated_output(
    ctx: ToolContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_invoke(*args, cwd, env, timeout_s=None, max_output_bytes):
        del args, cwd, env, max_output_bytes
        return make_proc_result(["git"], cwd=ctx.root, stdout="diff", stdout_truncated=True)

    monkeypatch.setattr(ctx.git, "_invoke", fake_invoke)

    result = await git_tools._git_diff_handler(git_tools.GitDiffInput(), ctx)

    assert result.ok is True
    assert result.content == "diff\n[output truncated]"


async def test_git_commit_re_raises_an_unrecognised_git_error(
    ctx: ToolContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_commit(*args, **kwargs):
        raise GitError(
            ("commit",),
            make_proc_result(["git"], cwd=ctx.root, returncode=1, stderr="broken"),
        )

    monkeypatch.setattr(ctx.git, "commit", fake_commit)

    with pytest.raises(GitError):
        await git_tools._git_commit_handler(git_tools.GitCommitInput(message="x"), ctx)
