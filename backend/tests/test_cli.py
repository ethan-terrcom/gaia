"""Tests for the ``gaia`` command line entry point (doc 02, task 5)."""

from __future__ import annotations

import importlib.metadata
import stat
from pathlib import Path

import pytest

import gaia
from gaia import cli
from gaia.config.secrets import InMemorySecretStore, SecretName
from gaia.config.settings import Settings

DUMMY_ANTHROPIC = "dummy" + "-anthropic-value"


@pytest.fixture(autouse=True)
def data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the CLI at a temporary data directory, never at ~/.gaia."""
    directory = tmp_path / "gaia-data"
    monkeypatch.setattr(cli, "get_settings", lambda: Settings(data_dir=directory))
    return directory


@pytest.fixture(autouse=True)
def store(monkeypatch: pytest.MonkeyPatch) -> InMemorySecretStore:
    """Keep every CLI secret operation in memory."""
    fake_store = InMemorySecretStore()
    monkeypatch.setattr(cli, "get_secret_store", lambda: fake_store)
    return fake_store


@pytest.fixture(autouse=True)
def prompts(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Answer getpass without a terminal, and record the prompts that were shown."""
    shown: list[str] = []

    def fake_getpass(prompt: str = "") -> str:
        shown.append(prompt)
        return DUMMY_ANTHROPIC

    monkeypatch.setattr("getpass.getpass", fake_getpass)
    return shown


def test_no_arguments_prints_the_help(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as excinfo:
        cli.main([])

    assert excinfo.value.code == 2
    assert "usage: gaia" in capsys.readouterr().err


def test_version_prints_the_package_version(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as excinfo:
        cli.main(["--version"])

    assert excinfo.value.code == 0
    assert capsys.readouterr().out.strip() == gaia.__version__


def test_version_does_not_touch_the_data_directory(data_dir: Path) -> None:
    with pytest.raises(SystemExit):
        cli.main(["--version"])

    assert not data_dir.exists()


def test_the_console_script_is_installed() -> None:
    scripts = {
        script.name: script.value
        for script in importlib.metadata.entry_points(group="console_scripts")
    }

    assert scripts["gaia"] == "gaia.cli:main"


def test_secrets_set_prompts_and_stores(
    capsys: pytest.CaptureFixture[str],
    store: InMemorySecretStore,
    prompts: list[str],
    data_dir: Path,
) -> None:
    assert cli.main(["secrets", "set", "ANTHROPIC_API_KEY"]) == 0

    assert store.require(SecretName.ANTHROPIC_API_KEY).get_secret_value() == DUMMY_ANTHROPIC
    captured = capsys.readouterr()
    assert "Stored ANTHROPIC_API_KEY in the keychain." in captured.out
    # The prompt names the secret and never echoes the value.
    assert any("ANTHROPIC_API_KEY" in prompt for prompt in prompts)
    assert DUMMY_ANTHROPIC not in captured.out + captured.err
    assert data_dir.is_dir()


def test_secrets_set_accepts_any_known_name(store: InMemorySecretStore) -> None:
    assert cli.main(["secrets", "set", "DEEPSEEK_API_KEY"]) == 0

    assert store.get(SecretName.DEEPSEEK_API_KEY) is not None


def test_secrets_set_rejects_an_unknown_name(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as excinfo:
        cli.main(["secrets", "set", "NOT_A_SECRET"])

    assert excinfo.value.code == 2
    assert "NOT_A_SECRET" in capsys.readouterr().err


def test_secrets_set_refuses_the_api_token(
    capsys: pytest.CaptureFixture[str], store: InMemorySecretStore, prompts: list[str]
) -> None:
    assert cli.main(["secrets", "set", "GAIA_API_TOKEN"]) == 1

    assert store.get(SecretName.GAIA_API_TOKEN) is None
    assert prompts == []
    assert "GAIA_API_TOKEN" in capsys.readouterr().err


def test_secrets_set_rejects_an_empty_value(
    capsys: pytest.CaptureFixture[str],
    store: InMemorySecretStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("getpass.getpass", lambda prompt="": "   ")

    assert cli.main(["secrets", "set", "ANTHROPIC_API_KEY"]) == 1

    assert store.get(SecretName.ANTHROPIC_API_KEY) is None
    assert "empty" in capsys.readouterr().err


def test_secrets_list_shows_names_but_never_values(
    capsys: pytest.CaptureFixture[str], store: InMemorySecretStore
) -> None:
    store.set(SecretName.ANTHROPIC_API_KEY, DUMMY_ANTHROPIC)

    assert cli.main(["secrets", "list"]) == 0

    captured = capsys.readouterr()
    assert "ANTHROPIC_API_KEY" in captured.out
    assert "set" in captured.out
    assert "GITHUB_TOKEN" in captured.out
    assert DUMMY_ANTHROPIC not in captured.out + captured.err


def test_secrets_alone_prints_the_help(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as excinfo:
        cli.main(["secrets"])

    assert excinfo.value.code == 2
    assert "secrets" in capsys.readouterr().err


def test_secrets_delete_removes_the_value(
    capsys: pytest.CaptureFixture[str], store: InMemorySecretStore
) -> None:
    store.set(SecretName.GITHUB_TOKEN, DUMMY_ANTHROPIC)

    assert cli.main(["secrets", "delete", "GITHUB_TOKEN"]) == 0

    assert store.get(SecretName.GITHUB_TOKEN) is None
    assert "Deleted GITHUB_TOKEN from the keychain." in capsys.readouterr().out


def test_secrets_delete_of_an_unset_name_is_an_error(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert cli.main(["secrets", "delete", "GITHUB_TOKEN"]) == 1

    assert "not set" in capsys.readouterr().err


def test_secrets_set_creates_a_private_data_directory(data_dir: Path) -> None:
    assert cli.main(["secrets", "set", "GITHUB_TOKEN"]) == 0

    assert stat.S_IMODE(data_dir.stat().st_mode) == 0o700
