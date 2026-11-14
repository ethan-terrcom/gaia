"""CLI tests for ``gaia github check`` (doc 15a)."""

from __future__ import annotations

import argparse
from pathlib import Path

import pytest
from pydantic import SecretStr

from gaia import cli
from gaia.config.secrets import InMemorySecretStore, SecretName
from gaia.config.settings import Settings
from gaia.db.engine import session_scope
from gaia.db.models import Repository
from gaia.db.repo import ScopedRepo
from gaia.services import github as github_module
from gaia.services.github import TokenCheck
from tests.db_builders import make_workspace

TOKEN = "github_pat_planted_0000000000000000000000000000000000"


class FakeGitHubClient:
    """An async-context-manager stand-in for the CLI's GitHub client."""

    def __init__(self, token: SecretStr) -> None:
        self.token = token

    async def __aenter__(self) -> FakeGitHubClient:
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        return None


@pytest.fixture(autouse=True)
def cli_env(
    tmp_path: Path,
    database,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[InMemorySecretStore, Settings]:
    """Point the CLI at the test DB/keychain and install a fake GitHub client."""
    store = InMemorySecretStore()
    store.set(SecretName.GITHUB_TOKEN, TOKEN)
    settings = Settings(data_dir=tmp_path / "gaia-data", db_url=database.url)
    monkeypatch.setattr(cli, "get_settings", lambda: settings)
    monkeypatch.setattr(cli, "get_secret_store", lambda: store)
    monkeypatch.setattr(github_module, "GitHubClient", FakeGitHubClient)
    return store, settings


async def _seed_local_repo(
    database, *, remote_url: str | None = "https://github.com/acme/widgets.git"
) -> None:
    async with session_scope(database.sessions) as session:
        workspace = await make_workspace(session, slug="local")
        await ScopedRepo(session, Repository, workspace.id).create(
            name="repo-gh",
            local_path="/tmp/gh/repo",
            remote_url=remote_url,
        )


async def test_cli_github_check_exit_0_when_all_present(
    database,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _seed_local_repo(database)

    async def fake_check_token(client: FakeGitHubClient, slug: object) -> TokenCheck:
        return TokenCheck(ok=True, missing=())

    monkeypatch.setattr(github_module, "check_token", fake_check_token)

    assert await cli._github_check_async(argparse.Namespace(repo="repo-gh")) == 0

    captured = capsys.readouterr()
    assert "Metadata R" in captured.out
    assert "Contents R/W" in captured.out
    assert "Pull requests R" in captured.out
    assert "Issues R" in captured.out
    assert "Commit statuses R" in captured.out
    assert "Checks R" in captured.out
    assert "missing" not in captured.out
    assert TOKEN not in captured.out + captured.err


async def test_cli_github_check_reports_an_unknown_repository(
    database,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_check_token(client: FakeGitHubClient, slug: object) -> TokenCheck:
        return TokenCheck(ok=True, missing=())

    monkeypatch.setattr(github_module, "check_token", fake_check_token)

    assert await cli._github_check_async(argparse.Namespace(repo="missing")) == 1

    captured = capsys.readouterr()
    assert "no repository named 'missing'" in captured.err
    assert TOKEN not in captured.out + captured.err


async def test_cli_github_check_reports_a_missing_remote(
    database,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _seed_local_repo(database, remote_url=None)

    async def fake_check_token(client: FakeGitHubClient, slug: object) -> TokenCheck:
        return TokenCheck(ok=True, missing=())

    monkeypatch.setattr(github_module, "check_token", fake_check_token)

    assert await cli._github_check_async(argparse.Namespace(repo="repo-gh")) == 1

    captured = capsys.readouterr()
    assert "no GitHub remote" in captured.err
    assert TOKEN not in captured.out + captured.err


async def test_cli_github_check_reports_an_unsupported_remote(
    database,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _seed_local_repo(database, remote_url="https://gitlab.com/acme/widgets.git")

    async def fake_check_token(client: FakeGitHubClient, slug: object) -> TokenCheck:
        return TokenCheck(ok=True, missing=())

    monkeypatch.setattr(github_module, "check_token", fake_check_token)

    assert await cli._github_check_async(argparse.Namespace(repo="repo-gh")) == 1

    captured = capsys.readouterr()
    assert "no GitHub remote" in captured.err
    assert TOKEN not in captured.out + captured.err


async def test_cli_github_check_reports_a_missing_secret(
    database,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _seed_local_repo(database)
    empty_store = InMemorySecretStore()
    monkeypatch.setattr(cli, "get_secret_store", lambda: empty_store)

    assert await cli._github_check_async(argparse.Namespace(repo="repo-gh")) == 1

    captured = capsys.readouterr()
    assert "GITHUB_TOKEN is not set" in captured.err
    assert TOKEN not in captured.out + captured.err


def test_github_dispatches_check(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "_github_check", lambda args: 5)

    assert cli._github(argparse.Namespace(github_command="check")) == 5


def test_github_unknown_command() -> None:
    assert cli._github(argparse.Namespace(github_command="wat")) == 2


def test_github_check_runs_the_async_body_with_asyncio(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_async(args: argparse.Namespace) -> int:
        return 3

    monkeypatch.setattr(cli, "_github_check_async", fake_async)

    assert cli._github_check(argparse.Namespace(repo="repo-gh")) == 3


async def test_cli_github_check_exit_1_when_one_missing(
    database,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _seed_local_repo(database)

    async def fake_check_token(client: FakeGitHubClient, slug: object) -> TokenCheck:
        return TokenCheck(ok=False, missing=("Issues R",))

    monkeypatch.setattr(github_module, "check_token", fake_check_token)

    assert await cli._github_check_async(argparse.Namespace(repo="repo-gh")) == 1

    captured = capsys.readouterr()
    assert "Issues R" in captured.out
    assert "missing" in captured.out
    assert "Contents R/W" in captured.out
    assert "fine-grained PAT scopes" in captured.err
    assert TOKEN not in captured.out + captured.err
