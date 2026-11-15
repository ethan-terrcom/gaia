"""Tests for ``gaia demo init`` and the demo-mode settings override."""

from __future__ import annotations

import argparse
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import select

from gaia import cli
from gaia.config.settings import Settings
from gaia.db.engine import session_scope
from gaia.db.models import Repository, Ticket, Workspace
from gaia.db.repo import ScopedRepo
from gaia.dev.demo import init_demo
from gaia.domain.enums import RuntimeKind
from gaia.services.git import GitService


def test_fake_llm_forces_both_runtimes_to_native_deepseek(
    capsys: pytest.CaptureFixture[str],
) -> None:
    settings = Settings(fake_llm=True)

    assert settings.fake_llm is True
    assert settings.lead_runtime is RuntimeKind.NATIVE_DEEPSEEK
    assert settings.junior_runtime is RuntimeKind.NATIVE_DEEPSEEK
    captured = capsys.readouterr()
    assert "gaia.demo_mode_runtime_override" in captured.out or (
        "gaia.demo_mode_runtime_override" in captured.err
    )


def test_gaia_fake_llm_is_read_from_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GAIA_FAKE_LLM", "1")

    settings = Settings()

    assert settings.fake_llm is True
    assert settings.lead_runtime is RuntimeKind.NATIVE_DEEPSEEK
    assert settings.junior_runtime is RuntimeKind.NATIVE_DEEPSEEK


def test_fake_llm_native_runtimes_do_not_log_an_override(
    capsys: pytest.CaptureFixture[str],
) -> None:
    settings = Settings(
        fake_llm=True,
        lead_runtime=RuntimeKind.NATIVE_DEEPSEEK,
        junior_runtime=RuntimeKind.NATIVE_DEEPSEEK,
    )

    assert settings.lead_runtime is RuntimeKind.NATIVE_DEEPSEEK
    assert settings.junior_runtime is RuntimeKind.NATIVE_DEEPSEEK
    captured = capsys.readouterr()
    assert "gaia.demo_mode_runtime_override" not in captured.out
    assert "gaia.demo_mode_runtime_override" not in captured.err


def test_demo_init_cli_handler_runs_the_async_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_calls: list[Any] = []

    def fake_run(coro: Any) -> int:
        run_calls.append(coro)
        coro.close()
        return 5

    monkeypatch.setattr(cli, "get_settings", lambda: object())
    monkeypatch.setattr(cli, "asyncio", SimpleNamespace(run=fake_run))

    assert cli._demo_init(argparse.Namespace()) == 5
    assert len(run_calls) == 1


def test_demo_command_dispatches_init_and_rejects_unknown_actions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cli, "_demo_init", lambda args: 7)

    assert cli._demo(argparse.Namespace(demo_command="init")) == 7
    assert cli._demo(argparse.Namespace(demo_command="unknown")) == 2


def _run_demo_tests(repo_path: Path) -> subprocess.CompletedProcess[str]:
    """Run the demo repository's test command on the host."""
    return subprocess.run(
        ["python3", "-m", "unittest", "-q"],
        cwd=repo_path,
        capture_output=True,
        text=True,
        check=False,
    )


async def test_demo_init_is_idempotent_and_its_tests_pass_on_the_host(
    tmp_path: Path,
    database: Any,
    capsys: pytest.CaptureFixture[str],
) -> None:
    settings = Settings(data_dir=tmp_path / "gaia-data", db_url=database.url)
    repo_path = settings.data_dir / "demo" / "greet"

    assert await init_demo(settings) == 0
    first_repository_id, first_ticket_id = await _demo_ids(database, repo_path)

    assert await init_demo(settings) == 0
    second_repository_id, second_ticket_id = await _demo_ids(database, repo_path)

    assert first_repository_id == second_repository_id
    assert first_ticket_id == second_ticket_id

    git = GitService(settings)
    commits = await git.log(repo_path)
    assert len(commits) == 1
    assert commits[0].summary == "demo: add greet() and its test"

    completed = _run_demo_tests(repo_path)
    assert completed.returncode == 0, completed.stderr
    assert "Ran 1 test" in completed.stderr

    output = capsys.readouterr().out
    assert "demo repository:" in output
    assert "make demo" in output


async def _demo_ids(database: Any, repo_path: Path) -> tuple[str, str]:
    """Return the demo repository and ticket ids, asserting one of each."""
    async with session_scope(database.sessions) as session:
        workspace = (
            await session.scalars(select(Workspace).where(Workspace.slug == "local"))
        ).one()
        repositories = ScopedRepo(session, Repository, workspace.id)
        repository_rows = await repositories.list(local_path=str(repo_path))
        assert len(repository_rows) == 1
        repository = repository_rows[0]

        tickets = ScopedRepo(session, Ticket, workspace.id)
        ticket_rows = await tickets.list(repo_id=repository.id)
        assert len(ticket_rows) == 1
        return repository.id, ticket_rows[0].id
