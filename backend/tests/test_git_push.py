"""Doc 15a git push: token in env only, no hooks, no force, strict URL rule."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from pydantic import SecretStr

import gaia.services.git as git_module
from gaia.services.git import ASKPASS_CONTENT, GitError, GitService, InvalidRef, WorktreeLocation
from gaia.services.proc import ProcResult, run_proc

TOKEN = "github_pat_planted_0000000000000000000000000000000000"
BRANCH = "gaia/15a-test"


def _bare_repo(tmp_path: Path) -> Path:
    """Create a bare repository for a no-network push target."""
    bare = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True)
    return bare


async def _worktree_location(
    git_service: GitService, git_repo: Path, tmp_path: Path
) -> WorktreeLocation:
    """Create a linked worktree on ``gaia/15a-test`` and return its pin."""
    work_tree = tmp_path / "worktree"
    await git_service.worktree_add(git_repo, path=work_tree, ref="main", new_branch=BRANCH)
    git_dir = await git_service.worktree_admin_dir(git_repo, work_tree)
    return WorktreeLocation(work_tree=work_tree, git_dir=git_dir)


def _bare_has_branch(bare: Path, branch: str) -> bool:
    completed = subprocess.run(
        ["git", "--git-dir", str(bare), "rev-parse", "--verify", f"refs/heads/{branch}"],
        capture_output=True,
        text=True,
        check=False,
    )
    return completed.returncode == 0


async def test_push_to_a_local_bare_repo_succeeds(
    git_service: GitService, git_repo: Path, tmp_path: Path
) -> None:
    bare = _bare_repo(tmp_path)
    location = await _worktree_location(git_service, git_repo, tmp_path)

    await git_service._push(
        location,
        url=bare.as_uri(),
        branch=BRANCH,
        token=SecretStr(TOKEN),
        timeout_s=30,
    )

    assert _bare_has_branch(bare, BRANCH)


async def test_push_refuses_a_non_gaia_branch(git_service: GitService, tmp_path: Path) -> None:
    location = WorktreeLocation(work_tree=tmp_path, git_dir=tmp_path)

    with pytest.raises(InvalidRef, match="must start with 'gaia/'"):
        await git_service.push(
            location,
            url="https://github.com/acme/widgets.git",
            branch="main",
            token=SecretStr(TOKEN),
        )


@pytest.mark.parametrize(
    "url",
    [
        "https://user:github_pat_planted@github.com/acme/widgets.git",
        "https://gitlab.com/acme/widgets.git",
        "file:///tmp/acme/widgets.git",
        "https://github.com.acme.invalid/acme/widgets.git",
    ],
)
def test_validate_push_url_refuses_credentials_and_non_github_hosts(url: str) -> None:
    with pytest.raises(ValueError, match="push URL"):
        git_module.validate_push_url(url)


def test_validate_push_url_accepts_an_https_github_url() -> None:
    url = "https://github.com/acme/widgets.git"
    assert git_module.validate_push_url(url) == url


def test_validate_push_url_refuses_an_at_after_the_github_prefix() -> None:
    with pytest.raises(ValueError, match="must not contain credentials"):
        git_module.validate_push_url("https://github.com/acme/widgets.git?token=x@y")


def test_push_argv_has_no_token_and_no_force(git_service: GitService, tmp_path: Path) -> None:
    argv = git_service._push_argv("https://github.com/acme/widgets.git", BRANCH)

    assert TOKEN not in " ".join(argv)
    assert "--force" not in argv
    assert "credential.helper=" in argv
    assert any(argument.startswith("core.askPass=") for argument in argv)
    assert "push.default=nothing" in argv
    assert "push" in argv
    assert "--no-verify" in argv
    assert "--porcelain" in argv
    assert "https://github.com/acme/widgets.git" in argv
    assert f"refs/heads/{BRANCH}:refs/heads/{BRANCH}" in argv


async def test_push_passes_the_token_only_through_gaia_git_token(
    git_service: GitService,
    git_repo: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    location = await _worktree_location(git_service, git_repo, tmp_path)
    seen: dict[str, object] = {}

    async def recording(
        argv: list[str],
        cwd: Path,
        timeout_s: float,
        env: dict[str, str] | None = None,
        max_output_bytes: int = 64 * 1024,
    ) -> ProcResult:
        seen["argv"] = tuple(argv)
        seen["env"] = dict(env or {})
        return ProcResult(
            argv=tuple(argv),
            cwd=cwd,
            pid=0,
            returncode=0,
            stdout="",
            stderr="",
            stdout_truncated=False,
            stderr_truncated=False,
            duration_s=0.0,
            timed_out=False,
        )

    monkeypatch.setattr(git_module, "run_proc", recording)

    await git_service._push(
        location,
        url="file:///tmp/acme/widgets.git",
        branch=BRANCH,
        token=SecretStr(TOKEN),
        timeout_s=30,
    )

    argv = str(seen["argv"])
    env = seen["env"]
    assert isinstance(env, dict)
    assert TOKEN not in argv
    assert "--force" not in argv
    assert env.get("GAIA_GIT_TOKEN") == TOKEN
    assert env.get("GIT_ASKPASS") == str(git_service._askpass_path())
    assert env.get("GIT_TERMINAL_PROMPT") == "0"
    for key, value in env.items():
        if key != "GAIA_GIT_TOKEN":
            assert value != TOKEN


async def test_a_pre_push_hook_in_the_source_worktree_does_not_run(
    git_service: GitService, git_repo: Path, tmp_path: Path
) -> None:
    marker = tmp_path / "pre-push-ran"
    hook = git_repo / ".git" / "hooks" / "pre-push"
    hook.write_text(f"#!/bin/sh\ntouch {marker}\nexit 1\n")
    hook.chmod(0o755)
    bare = _bare_repo(tmp_path)
    location = await _worktree_location(git_service, git_repo, tmp_path)

    await git_service._push(
        location,
        url=bare.as_uri(),
        branch=BRANCH,
        token=SecretStr(TOKEN),
        timeout_s=30,
    )

    assert not marker.exists(), "the source worktree's pre-push hook ran"
    assert _bare_has_branch(bare, BRANCH)


async def test_askpass_script_answers_both_prompts(git_service: GitService, tmp_path: Path) -> None:
    path = git_service._write_askpass()

    username = await run_proc(
        [str(path), "Username for 'https://github.com':"],
        cwd=tmp_path,
        env={"GAIA_GIT_TOKEN": TOKEN},
    )
    password = await run_proc(
        [str(path), "Password for 'https://github.com':"],
        cwd=tmp_path,
        env={"GAIA_GIT_TOKEN": TOKEN},
    )

    assert username.ok
    assert username.stdout.strip() == "x-access-token"
    assert password.ok
    assert password.stdout.strip() == TOKEN
    assert path.read_text() == ASKPASS_CONTENT


async def test_askpass_script_is_written_owner_only(
    git_service: GitService, tmp_path: Path
) -> None:
    path = git_service._write_askpass()

    assert path.stat().st_mode & 0o777 == 0o700


def test_askpass_is_left_alone_when_its_content_is_already_right(
    git_service: GitService,
) -> None:
    first = git_service._write_askpass()
    first_text = first.read_text()

    second = git_service._write_askpass()

    assert second == first
    assert first.read_text() == first_text == ASKPASS_CONTENT


async def test_a_failed_push_error_does_not_contain_the_token(
    git_service: GitService, git_repo: Path, tmp_path: Path
) -> None:
    location = await _worktree_location(git_service, git_repo, tmp_path)

    with pytest.raises(GitError) as caught:
        await git_service._push(
            location,
            url="file:///tmp/gaia-does-not-exist/widgets.git",
            branch=BRANCH,
            token=SecretStr(TOKEN),
            timeout_s=30,
        )

    assert TOKEN not in str(caught.value)


async def test_askpass_path_is_stable(git_service: GitService) -> None:
    assert git_service._askpass_path() == git_service.data_dir / "bin" / "gaia-askpass"


def test_push_argv_pins_the_settings_that_could_leak_the_token(
    git_service: GitService,
) -> None:
    """Lead review P4: the repository's own config is read during a push, so the
    settings that could send the token elsewhere are pinned on the command line."""
    argv = git_service._push_argv("https://github.com/acme/widgets.git", BRANCH)
    for pin in (
        "credential.helper=",
        "http.sslVerify=true",
        "http.proxy=",
        "http.extraHeader=",
        "http.followRedirects=false",
    ):
        assert pin in argv


async def test_push_refuses_a_url_rewrite_in_git_config(
    git_service: GitService, git_repo: Path, tmp_path: Path
) -> None:
    """Lead review P4: ``url.<x>.insteadOf`` could reroute the push, and the token
    with it, to another host or to SSH with the owner's keys."""
    bare = _bare_repo(tmp_path)
    location = await _worktree_location(git_service, git_repo, tmp_path)
    result = await run_proc(
        [
            "git",
            "-C",
            str(git_repo),
            "config",
            f"url.{bare.as_uri()}.insteadOf",
            "https://github.com/",
        ],
        cwd=git_repo,
    )
    assert result.ok

    with pytest.raises(git_module.UnsafePath, match="rewrites URLs"):
        await git_service._push(
            location, url=bare.as_uri(), branch=BRANCH, token=SecretStr(TOKEN), timeout_s=30
        )

    assert not _bare_has_branch(bare, BRANCH)
