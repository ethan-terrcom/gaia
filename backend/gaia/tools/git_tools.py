"""Git tools for agents (doc 09b task 3).

``run_command`` denies git entirely, so agents get these tools instead. Every
call goes through :class:`~gaia.services.git.GitService` and the pinned
:class:`~gaia.services.git.WorktreeLocation`, which is what keeps git off the
worktree's rewritable ``.git`` file and repo hooks (doc 05b finding F1).
"""

from __future__ import annotations

from pathlib import Path
from typing import cast

from pydantic import BaseModel, Field, model_validator

from gaia.security.paths import confine
from gaia.services.git import GitAuthor, GitError
from gaia.tools.registry import Tool, ToolContext, ToolInput, ToolResult

#: ``git_diff`` output is capped at the same size as search and command output.
GIT_DIFF_CAP_CHARS = 64_000


class GitStatusInput(ToolInput):
    """``git status`` takes no arguments."""


class GitDiffInput(ToolInput):
    path: str | None = None
    staged: bool = False


class GitCommitInput(ToolInput):
    message: str = Field(min_length=1, max_length=2000)

    @model_validator(mode="after")
    def _validate_message(self) -> GitCommitInput:
        if "\x00" in self.message:
            raise ValueError("message must not contain a NUL byte")
        first_line = self.message.splitlines()[0] if self.message.splitlines() else ""
        if len(first_line) > 200:
            raise ValueError("the first line of the message is longer than 200 characters")
        return self


async def _git_status_handler(input: BaseModel, ctx: ToolContext) -> ToolResult:
    del input
    status = await ctx.git.status_porcelain(ctx.task.worktree)
    return ToolResult(
        True,
        status if status else "clean",
        data={"clean": not bool(status)},
    )


async def _git_diff_handler(input: BaseModel, ctx: ToolContext) -> ToolResult:
    model = cast(GitDiffInput, input)
    target: str | None = None
    path: Path | None = None
    if model.path is not None:
        path = confine(ctx.root, model.path)
        target = path.relative_to(ctx.root).as_posix()
    result = await ctx.git.diff_worktree(
        ctx.task.worktree,
        path=path,
        staged=model.staged,
        max_output_bytes=GIT_DIFF_CAP_CHARS,
    )
    if not result.ok:
        first_line = result.stderr.strip().splitlines()
        return ToolResult(
            False,
            first_line[0] if first_line else "git diff failed",
            data={"target": target, "reason": first_line[0] if first_line else "git diff failed"},
        )
    content = result.stdout
    if result.stdout_truncated:
        content += "\n[output truncated]"
    return ToolResult(True, content, data={"target": target, "staged": model.staged})


async def _git_commit_handler(input: BaseModel, ctx: ToolContext) -> ToolResult:
    model = cast(GitCommitInput, input)
    agent = ctx.actor.removeprefix("agent:")
    author = GitAuthor(name=f"Gaia ({agent})", email="gaia@localhost")
    try:
        sha = await ctx.git.commit(
            ctx.task.worktree,
            model.message,
            add_all=True,
            author=author,
        )
    except GitError as exc:
        output = f"{exc.result.stdout}\n{exc.result.stderr}"
        if "nothing to commit" in output:
            return ToolResult(False, "nothing to commit", data={"reason": "nothing to commit"})
        raise
    return ToolResult(
        True,
        sha[:8],
        data={"sha": sha, "short_sha": sha[:8]},
    )


git_status = Tool(
    name="git_status",
    description="Show the worktree status in git status --porcelain format; 'clean' when empty.",
    input_model=GitStatusInput,
    handler=_git_status_handler,
    mutates=False,
)

git_diff = Tool(
    name="git_diff",
    description=(
        "Show the working-tree diff (or staged diff with staged=true), "
        "optionally confined to one path."
    ),
    input_model=GitDiffInput,
    handler=_git_diff_handler,
    mutates=False,
)

git_commit = Tool(
    name="git_commit",
    description=(
        "Stage every change in the worktree and commit it with a Gaia author. "
        "The first message line is the summary and must be at most 200 characters."
    ),
    input_model=GitCommitInput,
    handler=_git_commit_handler,
    mutates=True,
)
