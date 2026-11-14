"""Doc 05 task 2: the hardened git service.

The point of these tests is not that ``git rev-parse`` works - it does - but that a
*hostile* repository cannot change what git does when Gaia calls it: no hook runs,
no signing dialog opens, and the author is Gaia rather than whatever the repo
config says.
"""

from __future__ import annotations

import shutil
import stat
from collections.abc import Mapping, Sequence
from pathlib import Path

import pytest

import gaia.services.git as git_module
import gaia.services.proc as proc_module
from gaia.config.settings import Settings
from gaia.services.git import (
    GITCONFIG_CONTENT,
    GITCONFIG_NAME,
    Commit,
    GitError,
    GitService,
    InvalidRef,
    NotARepository,
    UnsafePath,
    _parse_worktree_porcelain,
)
from gaia.services.proc import DEFAULT_MAX_OUTPUT_BYTES, DEFAULT_TIMEOUT_S, ProcResult
from tests.git_repo import commit_file, git, resolved


class Ticket:
    """A stand-in for a ``ticket`` row: ``branch_name_for`` needs two strings."""

    def __init__(self, id_: str, title: str) -> None:
        self.id = id_
        self.title = title


# --------------------------------------------------------------- plumbing --
def test_git_config_is_written_with_the_gaia_identity(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path / "gaia-data")

    service = GitService(settings)

    config = settings.data_dir / GITCONFIG_NAME
    assert config == service.config_path
    assert config.read_text() == GITCONFIG_CONTENT
    assert "name = Gaia" in config.read_text()
    assert "gpgsign = false" in config.read_text()
    # Owner-only: it names the identity Gaia commits with, and there is no reason
    # for anything else on the machine to read it.
    assert stat.S_IMODE(config.stat().st_mode) == 0o600


def test_git_config_is_repaired_when_it_was_edited(tmp_path: Path) -> None:
    """Gaia's git config is Gaia's: an edit is undone on the next construction."""
    settings = Settings(data_dir=tmp_path / "gaia-data")
    service = GitService(settings)
    service.config_path.write_text("[user]\n\temail = someone@else.invalid\n")

    GitService(settings)

    assert service.config_path.read_text() == GITCONFIG_CONTENT


def test_git_config_is_left_alone_when_it_is_already_right(tmp_path: Path) -> None:
    """The file is read, not rewritten, on every later start."""
    settings = Settings(data_dir=tmp_path / "gaia-data")
    service = GitService(settings)

    GitService(settings)

    assert service.config_path.read_text() == GITCONFIG_CONTENT
    assert stat.S_IMODE(service.config_path.stat().st_mode) == 0o600


def test_construction_creates_the_data_directory(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path / "fresh" / "gaia-data")

    GitService(settings)

    assert settings.data_dir.is_dir()


# ------------------------------------------------------------------ bounds --
async def test_a_per_command_timeout_overrides_the_service_default(
    git_repo: Path, git_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The right bound is a property of the command, not of the service.

    Doc 15's ``push`` of a large repo needs minutes where a request-path
    ``status`` wants seconds, so every command method takes ``timeout_s=``.
    Recorded at the one place it can be honoured - the ``run_proc`` call - and the
    real command still runs, so this checks the plumbing rather than a mock.
    """
    seen: list[float] = []
    real_run_proc = proc_module.run_proc

    async def recording(
        argv: Sequence[str],
        cwd: Path,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        env: Mapping[str, str] | None = None,
        max_output_bytes: int = DEFAULT_MAX_OUTPUT_BYTES,
    ) -> ProcResult:
        seen.append(timeout_s)
        return await real_run_proc(
            argv, cwd, timeout_s=timeout_s, env=env, max_output_bytes=max_output_bytes
        )

    monkeypatch.setattr(git_module, "run_proc", recording)
    service = GitService(git_settings, timeout_s=5.0)

    await service.status_porcelain(git_repo)
    await service.status_porcelain(git_repo, timeout_s=90.0)

    assert seen == [5.0, 90.0]


# ------------------------------------------------------------------ repos --
async def test_validate_repo_returns_the_resolved_root(
    git_repo: Path, git_service: GitService
) -> None:
    assert await git_service.validate_repo(git_repo) == resolved(git_repo)


async def test_validate_repo_accepts_a_subdirectory_and_returns_the_root(
    git_repo: Path, git_service: GitService
) -> None:
    """Registering ``repo/backend`` should not silently make it its own repo."""
    nested = git_repo / "backend"
    nested.mkdir()

    assert await git_service.validate_repo(nested) == resolved(git_repo)


async def test_validate_repo_rejects_a_directory_outside_git(
    tmp_path: Path, git_service: GitService
) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()

    with pytest.raises(NotARepository, match="is not a git repository root"):
        await git_service.validate_repo(plain)


async def test_validate_repo_rejects_a_missing_directory(
    tmp_path: Path, git_service: GitService
) -> None:
    with pytest.raises(NotARepository, match="is not a directory"):
        await git_service.validate_repo(tmp_path / "nope")


async def test_validate_repo_refuses_anything_under_the_worktrees_root(
    git_settings: Settings, git_service: GitService
) -> None:
    """A worktree is a checkout of a repo; registering one would nest worktrees."""
    inside = git_settings.worktrees_root / "repo-id"
    inside.mkdir(parents=True)

    with pytest.raises(UnsafePath, match="inside the worktrees root"):
        await git_service.validate_repo(inside)


async def test_current_sha_and_current_branch(git_repo: Path, git_service: GitService) -> None:
    expected = git(git_repo, "rev-parse", "HEAD").strip()

    assert await git_service.current_sha(git_repo) == expected
    assert len(expected) == 40
    assert await git_service.current_branch(git_repo) == "main"


async def test_current_branch_is_none_when_head_is_detached(
    git_repo: Path, git_service: GitService
) -> None:
    git(git_repo, "checkout", "-q", "--detach", "HEAD")

    assert await git_service.current_branch(git_repo) is None


async def test_current_sha_reports_an_unknown_revision(
    git_repo: Path, git_service: GitService
) -> None:
    with pytest.raises(GitError, match="rev-parse"):
        await git_service.current_sha(git_repo, rev="no-such-branch")


async def test_current_sha_rejects_a_revision_that_looks_like_an_option(
    git_repo: Path, git_service: GitService
) -> None:
    with pytest.raises(InvalidRef, match="read as an option"):
        await git_service.current_sha(git_repo, rev="--output=/tmp/pwned")


async def test_default_branch_uses_the_checked_out_branch_without_a_remote(
    git_repo: Path, git_service: GitService
) -> None:
    assert await git_service.default_branch(git_repo) == "main"


async def test_default_branch_prefers_origin_head(git_repo: Path, git_service: GitService) -> None:
    """With a remote, the remote's answer wins over the local branch."""
    sha = git(git_repo, "rev-parse", "HEAD").strip()
    git(git_repo, "update-ref", "refs/remotes/origin/trunk", sha)
    git(git_repo, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/trunk")

    assert await git_service.default_branch(git_repo) == "trunk"


async def test_default_branch_falls_back_to_main(git_repo: Path, git_service: GitService) -> None:
    git(git_repo, "checkout", "-q", "--detach", "HEAD")

    assert await git_service.default_branch(git_repo) == "main"


async def test_status_porcelain_is_empty_when_clean_and_lists_changes(
    git_repo: Path, git_service: GitService
) -> None:
    assert await git_service.status_porcelain(git_repo) == ""
    assert not await git_service.is_dirty(git_repo)

    (git_repo / "new.txt").write_text("untracked\n")

    assert "new.txt" in await git_service.status_porcelain(git_repo)
    assert await git_service.is_dirty(git_repo)


async def test_diff_shows_what_changed_between_two_commits(
    git_repo: Path, git_service: GitService
) -> None:
    base = await git_service.current_sha(git_repo)
    head = commit_file(git_repo, "feature.txt", "added\n", "add feature")

    diff = await git_service.diff(git_repo, base, head)

    assert "+added" in diff
    assert "diff --git a/feature.txt b/feature.txt" in diff


async def test_diff_can_be_capped(git_repo: Path, git_service: GitService) -> None:
    """Doc 14 shows a diff in the UI, so a huge one is cut, not buffered."""
    base = await git_service.current_sha(git_repo)
    head = commit_file(git_repo, "big.txt", "x" * 50_000 + "\n", "add a big file")

    diff = await git_service.diff(git_repo, base, head, max_output_bytes=500)

    assert len(diff) == 500


async def test_log_returns_newest_first(git_repo: Path, git_service: GitService) -> None:
    commit_file(git_repo, "a.txt", "a\n", "second")
    third = commit_file(git_repo, "b.txt", "b\n", "third")

    commits = await git_service.log(git_repo)

    assert [commit.summary for commit in commits] == ["third", "second", "initial commit"]
    assert commits[0] == Commit(sha=third, summary="third")
    assert all(len(commit.sha) == 40 for commit in commits)


async def test_log_honours_the_limit(git_repo: Path, git_service: GitService) -> None:
    commit_file(git_repo, "a.txt", "a\n", "second")

    assert [commit.summary for commit in await git_service.log(git_repo, limit=1)] == ["second"]


async def test_log_rejects_a_bad_limit(git_repo: Path, git_service: GitService) -> None:
    with pytest.raises(ValueError, match="limit must be positive"):
        await git_service.log(git_repo, limit=0)


# ------------------------------------------------------- hardened commits --
async def test_commit_does_not_run_a_repo_hook(git_repo: Path, git_service: GitService) -> None:
    """The doc's test: a ``pre-commit`` hook in the repo never runs."""
    marker = git_repo / "hook-ran"
    hook = git_repo / ".git" / "hooks" / "pre-commit"
    hook.write_text(f"#!/bin/sh\ntouch {marker}\nexit 1\n")
    hook.chmod(0o755)
    (git_repo / "work.txt").write_text("work\n")

    sha = await git_service.commit(git_repo, "04: work")

    assert not marker.exists(), "the repo's pre-commit hook ran"
    assert sha == git(git_repo, "rev-parse", "HEAD").strip()
    assert "work.txt" in git(git_repo, "show", "--name-only", "--pretty=format:", "HEAD")


async def test_commit_does_not_run_a_hooks_path_directory(
    git_repo: Path, git_service: GitService
) -> None:
    """The husky case: ``core.hooksPath`` in the repo's *tracked* config."""
    husky = git_repo / ".husky"
    husky.mkdir()
    marker = git_repo / "husky-ran"
    hook = husky / "pre-commit"
    hook.write_text(f"#!/bin/sh\ntouch {marker}\nexit 1\n")
    hook.chmod(0o755)
    git(git_repo, "config", "core.hooksPath", ".husky")
    (git_repo / "work.txt").write_text("work\n")

    await git_service.commit(git_repo, "04: work")

    assert not marker.exists(), "the repo's core.hooksPath hook ran"


async def test_commit_ignores_a_repo_that_turns_on_signing(
    git_repo: Path, git_service: GitService
) -> None:
    """``commit.gpgsign=true`` in the repo would spawn pinentry and fail."""
    git(git_repo, "config", "commit.gpgsign", "true")
    (git_repo / "work.txt").write_text("work\n")

    await git_service.commit(git_repo, "04: work")

    assert await git_service.current_sha(git_repo) == git(git_repo, "rev-parse", "HEAD").strip()


async def test_commit_is_authored_by_gaia_not_by_the_repo(
    git_repo: Path, git_service: GitService
) -> None:
    """A repo-local identity beats ``GIT_CONFIG_GLOBAL``, so the command line wins."""
    git(git_repo, "config", "user.name", "An Agent")
    git(git_repo, "config", "user.email", "agent@example.invalid")
    (git_repo / "work.txt").write_text("work\n")

    await git_service.commit(git_repo, "04: work")

    author = git(git_repo, "log", "-1", "--pretty=format:%an <%ae>")
    assert author == "Gaia <gaia@localhost>"


async def test_commit_can_skip_staging(git_repo: Path, git_service: GitService) -> None:
    """Doc 09b stages exactly what the agent wrote, then commits with ``add_all=False``."""
    (git_repo / "staged.txt").write_text("staged\n")
    git(git_repo, "add", "staged.txt")
    (git_repo / "staged.txt").write_text("staged and edited\n")

    await git_service.commit(git_repo, "04: work", add_all=False)

    assert git(git_repo, "show", "--name-only", "--pretty=format:", "HEAD").strip() == "staged.txt"
    assert await git_service.is_dirty(git_repo)


async def test_commit_rejects_an_empty_message(git_repo: Path, git_service: GitService) -> None:
    with pytest.raises(ValueError, match="cannot be empty"):
        await git_service.commit(git_repo, "   ")


# -------------------------------------------------------------- branches --
def test_branch_name_for_uses_the_id_tail_and_a_slug(git_service: GitService) -> None:
    """``gaia/<short-id>-<slug>``, with the *random* tail of the UUIDv7."""
    ticket = Ticket("0190a5c0-7b1e-7f2a-9c3d-1a2b3c4d5e6f", "Add API skeleton & auth!")

    assert git_service.branch_name_for(ticket) == "gaia/3c4d5e6f-add-api-skeleton-auth"


def test_branch_names_differ_for_tickets_created_in_the_same_millisecond(
    git_service: GitService,
) -> None:
    """The first eight characters of a UUIDv7 are a timestamp: do not use them."""
    early = Ticket("0190a5c0-7b1e-7f2a-9c3d-1a2b3c4d5e6f", "One")
    late = Ticket("0190a5c0-7b1e-7f2a-9c3d-ffffffffffff", "One")

    assert git_service.branch_name_for(early) != git_service.branch_name_for(late)


def test_branch_name_slug_is_capped_at_forty_characters(git_service: GitService) -> None:
    ticket = Ticket("0190a5c0-7b1e-7f2a-9c3d-1a2b3c4d5e6f", "a" * 30 + " " + "b" * 30)

    slug = git_service.branch_name_for(ticket).removeprefix("gaia/3c4d5e6f-")

    assert len(slug) == 40
    assert not slug.endswith("-")


def test_branch_name_folds_accents_and_falls_back_when_nothing_is_left(
    git_service: GitService,
) -> None:
    accented = Ticket("0190a5c0-7b1e-7f2a-9c3d-1a2b3c4d5e6f", "Café ☕")
    symbolic = Ticket("0190a5c0-7b1e-7f2a-9c3d-1a2b3c4d5e6f", "🎉")

    assert git_service.branch_name_for(accented).endswith("-cafe")
    assert git_service.branch_name_for(symbolic).endswith("-work")


def test_branch_name_refuses_an_id_with_nothing_usable_in_it(
    git_service: GitService,
) -> None:
    with pytest.raises(InvalidRef, match="no characters usable"):
        git_service.branch_name_for(Ticket("----", "A ticket"))


async def test_a_caller_supplied_variable_reaches_git(
    git_repo: Path, git_service: GitService
) -> None:
    """Doc 15's push passes its token through the environment, not argv."""
    result = await git_service._run(
        "var", "GIT_AUTHOR_IDENT", cwd=git_repo, env={"GIT_AUTHOR_NAME": "Agent Smith"}
    )

    assert result.stdout.startswith("Agent Smith <")


async def test_validate_ref_accepts_what_git_accepts(git_service: GitService) -> None:
    assert await git_service.validate_ref("gaia/3c4d5e6f-add-api") == "gaia/3c4d5e6f-add-api"


@pytest.mark.parametrize(
    ("name", "match"),
    [
        ("gaia/bad..ref", "is not a valid branch name"),
        ("gaia/bad ref", "whitespace or a control character"),
        ("-x", "read as an option"),
        ("", "cannot be empty"),
        ("gaia/lock.lock", "is not a valid branch name"),
    ],
)
async def test_validate_ref_rejects_what_git_rejects(
    git_service: GitService, name: str, match: str
) -> None:
    with pytest.raises(InvalidRef, match=match):
        await git_service.validate_ref(name)


async def test_branch_exists(git_repo: Path, git_service: GitService) -> None:
    assert await git_service.branch_exists(git_repo, "main")
    assert not await git_service.branch_exists(git_repo, "gaia/never-created")


# ------------------------------------------------------------- worktrees --
async def test_worktree_list_reports_the_main_worktree(
    git_repo: Path, git_service: GitService
) -> None:
    (entry,) = await git_service.worktree_list(git_repo)

    assert entry.path == resolved(git_repo)
    assert entry.branch == "main"
    assert entry.head == git(git_repo, "rev-parse", "HEAD").strip()
    assert not entry.bare
    assert not entry.detached
    assert not entry.prunable


async def test_worktree_add_creates_a_branch_and_remove_takes_it_away(
    git_repo: Path, git_service: GitService, tmp_path: Path
) -> None:
    worktree = tmp_path / "worktree"

    await git_service.worktree_add(git_repo, path=worktree, ref="main", new_branch="gaia/x")
    added = {entry.branch for entry in await git_service.worktree_list(git_repo)}
    assert added == {"main", "gaia/x"}
    assert (worktree / "README.md").is_file()

    await git_service.worktree_remove(git_repo, worktree)
    await git_service.worktree_prune(git_repo)

    assert [entry.branch for entry in await git_service.worktree_list(git_repo)] == ["main"]
    assert not worktree.exists()


async def test_worktree_add_can_check_out_an_existing_branch(
    git_repo: Path, git_service: GitService, tmp_path: Path
) -> None:
    """A run resumed on the same branch checks it out instead of recreating it."""
    first = tmp_path / "first"
    second = tmp_path / "second"
    await git_service.worktree_add(git_repo, path=first, ref="main", new_branch="gaia/x")
    await git_service.worktree_remove(git_repo, first)

    await git_service.worktree_add(git_repo, path=second, ref="gaia/x")

    assert second.is_dir()
    assert await git_service.current_branch(second) == "gaia/x"
    assert await git_service.branch_exists(git_repo, "gaia/x")


async def test_worktree_prune_forgets_a_directory_that_vanished(
    git_repo: Path, git_service: GitService, tmp_path: Path
) -> None:
    worktree = tmp_path / "worktree"
    await git_service.worktree_add(git_repo, path=worktree, ref="main", new_branch="gaia/x")
    shutil.rmtree(worktree)

    await git_service.worktree_prune(git_repo)

    assert [entry.branch for entry in await git_service.worktree_list(git_repo)] == ["main"]


async def test_worktree_add_requires_an_absolute_path(
    git_repo: Path, git_service: GitService
) -> None:
    with pytest.raises(UnsafePath, match="must be absolute"):
        await git_service.worktree_add(
            git_repo, path=Path("relative"), ref="main", new_branch="gaia/x"
        )


async def test_worktree_remove_requires_an_absolute_path(
    git_repo: Path, git_service: GitService
) -> None:
    with pytest.raises(UnsafePath, match="must be absolute"):
        await git_service.worktree_remove(git_repo, Path("relative"))


def test_porcelain_parser_ignores_unknown_keys_and_reads_flags() -> None:
    """Git grows keys; a new one is not a reason to stop listing worktrees."""
    output = (
        "worktree /repos/one\nHEAD " + "a" * 40 + "\nbranch refs/heads/main\n\n"
        "worktree /repos/two\nHEAD " + "b" * 40 + "\ndetached\nlock held\n\n"
        "worktree /repos/bare\nbare\n\n"
        "worktree /repos/gone\nHEAD " + "c" * 40 + "\nbranch refs/heads/gaia/x\nprunable\n"
    )

    entries = _parse_worktree_porcelain(output)

    assert [entry.path.as_posix() for entry in entries] == [
        "/repos/one",
        "/repos/two",
        "/repos/bare",
        "/repos/gone",
    ]
    assert entries[0].branch == "main"
    assert entries[1].branch is None
    assert entries[1].detached
    assert entries[2].bare
    assert entries[3].prunable
    assert entries[3].branch == "gaia/x"


# ----------------------------------------------------------------- errors --
async def test_git_error_carries_the_first_line_of_stderr(
    git_repo: Path, git_service: GitService
) -> None:
    with pytest.raises(GitError) as caught:
        await git_service.current_sha(git_repo, rev="no-such-branch")

    message = str(caught.value)
    assert message.startswith("git rev-parse --verify no-such-branch^{commit} failed")
    assert "fatal:" in message  # git's own explanation survived, on one line
    assert "\n" not in message


async def test_git_error_redacts_a_token_from_stderr(
    git_repo: Path, git_service: GitService
) -> None:
    """Doc 15 pushes with a token; a failure message must not print it."""
    token = "ghp_" + "a" * 24

    with pytest.raises(GitError) as caught:
        await git_service.current_sha(git_repo, rev=token)

    assert token not in str(caught.value)
    assert "[REDACTED]" in str(caught.value)


def test_git_error_without_stderr_still_explains_itself() -> None:
    result = ProcResult(
        argv=("git", "push"),
        cwd=Path("/tmp"),
        pid=1,
        returncode=1,
        stdout="",
        stderr="",
        stdout_truncated=False,
        stderr_truncated=False,
        duration_s=0.0,
        timed_out=False,
    )

    assert str(GitError(("push",), result)) == "git push failed: git exited with status 1"
