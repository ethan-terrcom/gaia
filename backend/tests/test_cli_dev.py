"""Tests for `gaia dev fake-run` (doc 13)."""

from __future__ import annotations

import argparse
from pathlib import Path

import httpx
import pytest

from gaia import cli
from gaia.config.secrets import InMemorySecretStore, SecretName
from gaia.config.settings import Settings
from gaia.llm.base import LLMResponse, ToolCall
from gaia.runtimes import native_llm
from gaia.runtimes.native_llm import LlmHandle, UsageTally
from gaia.security import sandbox
from tests.conftest import Database
from tests.llm_fakes import make_usage
from tests.runtime_contract import make_worktree_location

DUMMY_TOKEN = "dummy" + "-api-token-value"


class _Response:
    def __init__(self, status_code: int, body: object) -> None:
        self.status_code = status_code
        self._body = body

    def json(self) -> object:
        if isinstance(self._body, Exception):
            raise self._body
        return self._body


@pytest.fixture
def settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Settings:
    value = Settings(data_dir=tmp_path / "gaia-data")
    monkeypatch.setattr(cli, "get_settings", lambda: value)
    return value


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch) -> InMemorySecretStore:
    fake = InMemorySecretStore()
    fake.set(SecretName.GAIA_API_TOKEN, DUMMY_TOKEN)
    monkeypatch.setattr(cli, "get_secret_store", lambda: fake)
    return fake


def test_dev_fake_run_posts_and_prints_the_run_id(
    settings: Settings,
    store: InMemorySecretStore,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: dict[str, object] = {}

    def fake_post(url: str, **kwargs: object) -> _Response:
        calls["url"] = url
        calls["headers"] = kwargs["headers"]
        calls["json"] = kwargs["json"]
        return _Response(200, {"run_id": "run-123"})

    monkeypatch.setattr(httpx, "post", fake_post)

    assert cli.main(["dev", "fake-run", "--events", "4", "--interval", "0.25"]) == 0

    captured = capsys.readouterr()
    assert captured.out.strip() == "run-123"
    assert DUMMY_TOKEN not in captured.out + captured.err
    assert calls["url"] == "http://127.0.0.1:8765/api/v1/dev/fake-run"
    assert calls["headers"] == {"Authorization": f"Bearer {DUMMY_TOKEN}"}
    assert calls["json"] == {"events": 4, "interval_s": 0.25}


def test_dev_fake_run_reports_a_connection_error_without_the_token(
    settings: Settings,
    store: InMemorySecretStore,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_post(url: str, **kwargs: object) -> _Response:
        raise httpx.ConnectError("boom")

    monkeypatch.setattr(httpx, "post", fake_post)

    assert cli.main(["dev", "fake-run"]) == 1

    captured = capsys.readouterr()
    assert "could not reach Gaia" in captured.err
    assert "boom" in captured.err
    assert DUMMY_TOKEN not in captured.out + captured.err


def test_dev_fake_run_reports_a_non_200_status(
    settings: Settings,
    store: InMemorySecretStore,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(httpx, "post", lambda url, **kwargs: _Response(422, {}))

    assert cli.main(["dev", "fake-run"]) == 1

    captured = capsys.readouterr()
    assert "Gaia answered 422" in captured.err
    assert DUMMY_TOKEN not in captured.out + captured.err


def test_dev_fake_run_reports_a_non_json_body(
    settings: Settings,
    store: InMemorySecretStore,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        httpx,
        "post",
        lambda url, **kwargs: _Response(200, ValueError("not json")),
    )

    assert cli.main(["dev", "fake-run"]) == 1

    captured = capsys.readouterr()
    assert "non-JSON" in captured.err
    assert DUMMY_TOKEN not in captured.out + captured.err


def test_dev_with_an_unknown_action_is_an_error() -> None:
    assert cli._dev(argparse.Namespace(dev_command="other")) == 2


def test_dev_run_task_dispatches(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[argparse.Namespace] = []

    def fake_run_task(args: argparse.Namespace) -> int:
        calls.append(args)
        return 0

    monkeypatch.setattr(cli, "_dev_run_task", fake_run_task)

    assert cli._dev(argparse.Namespace(dev_command="run-task")) == 0
    assert calls == [argparse.Namespace(dev_command="run-task")]


def test_dev_run_task_dispatches_native(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[argparse.Namespace, Settings]] = []

    async def fake_native_task(args: argparse.Namespace, settings: Settings) -> int:
        calls.append((args, settings))
        return 0

    monkeypatch.setattr(cli, "_run_native_task", fake_native_task)
    monkeypatch.setattr(cli, "get_settings", lambda: Settings(data_dir=Path("/tmp")))

    assert cli._dev_run_task(argparse.Namespace(runtime="native")) == 0
    assert calls[0][0] == argparse.Namespace(runtime="native")


def test_dev_run_task_prints_errors(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    async def fail(args: argparse.Namespace, settings: Settings) -> int:
        del args, settings
        raise ValueError("bad worktree")

    monkeypatch.setattr(cli, "_run_fake_task", fail)
    monkeypatch.setattr(cli, "get_settings", lambda: Settings(data_dir=Path("/tmp")))

    assert cli._dev_run_task(argparse.Namespace(worktree="/bad")) == 1
    assert "bad worktree" in capsys.readouterr().err


def test_worktree_location_parses_a_gaia_worktree(tmp_path: Path) -> None:
    work_tree = tmp_path / "worktree"
    work_tree.mkdir()
    git_dir = tmp_path / "admin"
    (work_tree / ".git").write_text(f"gitdir: {git_dir}\n")

    location = cli._worktree_location(work_tree)

    assert location.work_tree == work_tree.resolve()
    assert location.git_dir == git_dir.resolve()


@pytest.mark.parametrize("content", ["", "not-a-gitdir\n"])
def test_worktree_location_rejects_malformed_git_file(tmp_path: Path, content: str) -> None:
    work_tree = tmp_path / "worktree"
    work_tree.mkdir()
    (work_tree / ".git").write_text(content)

    with pytest.raises(ValueError, match="Gaia worktree"):
        cli._worktree_location(work_tree)


def test_worktree_location_rejects_a_missing_git_file(tmp_path: Path) -> None:
    work_tree = tmp_path / "worktree"
    work_tree.mkdir()

    with pytest.raises(ValueError, match=r"no \.git file"):
        cli._worktree_location(work_tree)


async def test_run_fake_task_end_to_end(
    database: Database,
    git_repo: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    worktree = make_worktree_location(git_repo, tmp_path)
    settings = Settings(data_dir=tmp_path / "gaia-data", db_url=database.url)
    args = argparse.Namespace(
        runtime="fake",
        worktree=str(worktree.work_tree),
        instructions="write hello.py",
    )

    assert await cli._run_fake_task(args, settings) == 0

    captured = capsys.readouterr()
    assert "completed" in captured.out
    assert (worktree.work_tree / "hello.py").exists()


async def test_run_native_task_end_to_end_with_scripted_llm(
    database: Database,
    git_repo: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worktree = make_worktree_location(git_repo, tmp_path)
    settings = Settings(data_dir=tmp_path / "gaia-data", db_url=database.url)
    args = argparse.Namespace(
        runtime="native",
        worktree=str(worktree.work_tree),
        instructions="finish immediately",
        model="deepseek-v4-pro",
        gateway_base=None,
    )

    monkeypatch.setattr(sandbox, "find_srt", lambda settings: tmp_path / "srt")

    def fake_llm_factory(**kwargs: object):
        del kwargs
        from tests.llm_fakes import ScriptedToolChatLLM

        llm = ScriptedToolChatLLM(
            [
                LLMResponse(
                    text="done",
                    usage=make_usage(),
                    tool_calls=(
                        ToolCall(
                            id="call_finish",
                            name="finish",
                            arguments={"summary": "finished", "tests_passed": True},
                        ),
                    ),
                )
            ]
        )

        async def factory(task: object) -> LlmHandle:
            del task
            return LlmHandle(llm=llm, usage=UsageTally(), close=_noop_close)

        return factory

    monkeypatch.setattr(native_llm, "gateway_llm_factory", fake_llm_factory)

    assert await cli._run_native_task(args, settings) == 0

    captured = capsys.readouterr()
    assert "completed" in captured.out
    assert "finished" in captured.out


async def _noop_close() -> None:
    return None
