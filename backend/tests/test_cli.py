"""Tests for the ``gaia`` command line entry point (doc 02, task 5)."""

from __future__ import annotations

import importlib.metadata
import stat
from pathlib import Path

import pytest

import gaia
from gaia import cli
from gaia.config import settings as settings_module
from gaia.config.secrets import InMemorySecretStore, SecretName
from gaia.config.settings import Settings

DUMMY_ANTHROPIC = "dummy" + "-anthropic-value"
DUMMY_TOKEN = "dummy" + "-api-token-value"

#: A bind address `Settings` refuses: docs 02 and 04 test that the server will not
#: listen on the whole network by accident. It is a value in a test, never a socket.
WILDCARD_HOST = "0.0.0.0"  # noqa: S104 - rejected input, never bound


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


def test_secrets_set_reports_a_closed_prompt(
    capsys: pytest.CaptureFixture[str],
    store: InMemorySecretStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ctrl-D (or no terminal) must exit 1 with a message, not a traceback."""

    def closed(prompt: str = "") -> str:
        raise EOFError

    monkeypatch.setattr("getpass.getpass", closed)

    assert cli.main(["secrets", "set", "ANTHROPIC_API_KEY"]) == 1

    assert "no value entered for ANTHROPIC_API_KEY" in capsys.readouterr().err
    assert store.get(SecretName.ANTHROPIC_API_KEY) is None


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


def test_ui_generates_the_token_and_prints_the_login_url(
    capsys: pytest.CaptureFixture[str], store: InMemorySecretStore, data_dir: Path
) -> None:
    """Doc 04 task 2: the UI gets its token from a fragment, never from argv."""
    assert store.get(SecretName.GAIA_API_TOKEN) is None

    assert cli.main(["ui"]) == 0

    token = store.require(SecretName.GAIA_API_TOKEN).get_secret_value()
    captured = capsys.readouterr()
    assert captured.out.strip() == f"http://127.0.0.1:5173/#token={token}"
    assert captured.err == ""
    assert data_dir.is_dir()


def test_ui_reuses_the_token_the_server_already_generated(
    capsys: pytest.CaptureFixture[str], store: InMemorySecretStore
) -> None:
    store.set(SecretName.GAIA_API_TOKEN, DUMMY_TOKEN)

    assert cli.main(["ui"]) == 0

    assert capsys.readouterr().out.strip().endswith(f"#token={DUMMY_TOKEN}")


def test_ui_follows_the_configured_ui_origin(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    settings = Settings(data_dir=tmp_path / "gaia-data", ui_origin="http://127.0.0.1:6000/")
    monkeypatch.setattr(cli, "get_settings", lambda: settings)

    assert cli.main(["ui"]) == 0

    # The trailing slash is not doubled.
    assert capsys.readouterr().out.startswith("http://127.0.0.1:6000/#token=")


def test_serve_passes_the_settings_to_uvicorn(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: dict[str, object] = {}

    def fake_run(app: str, **kwargs: object) -> None:
        calls["app"] = app
        calls.update(kwargs)

    monkeypatch.setattr("uvicorn.run", fake_run)

    assert cli.main(["serve"]) == 0

    assert calls == {
        "app": cli.APP_TARGET,
        "factory": True,
        "host": "127.0.0.1",
        "port": 8765,
        "reload": False,
    }


def test_serve_can_enable_reload(monkeypatch: pytest.MonkeyPatch) -> None:
    """`make dev-backend` passes --reload; a string target is what makes it work."""
    calls: dict[str, object] = {}
    monkeypatch.setattr("uvicorn.run", lambda app, **kwargs: calls.update(kwargs))

    assert cli.main(["serve", "--reload"]) == 0

    assert calls["reload"] is True
    assert cli.APP_TARGET == "gaia.api.app:create_app"


def test_serve_refuses_a_non_loopback_host(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Acceptance criterion: 0.0.0.0 is refused unless `allow_remote` is set."""
    monkeypatch.setenv("GAIA_HOST", WILDCARD_HOST)
    monkeypatch.setattr(cli, "get_settings", settings_module.get_settings)

    assert cli.main(["serve"]) == 2

    message = capsys.readouterr().err
    assert "not a loopback address" in message
    assert "allow_remote=True" in message
    assert "Traceback" not in message


def test_serve_binds_a_remote_host_when_the_owner_allows_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: dict[str, object] = {}
    monkeypatch.setenv("GAIA_HOST", WILDCARD_HOST)
    monkeypatch.setenv("GAIA_ALLOW_REMOTE", "true")
    monkeypatch.setenv("GAIA_DATA_DIR", str(tmp_path / "gaia-data"))
    monkeypatch.setattr(cli, "get_settings", settings_module.get_settings)
    monkeypatch.setattr("uvicorn.run", lambda app, **kwargs: calls.update(kwargs))

    assert cli.main(["serve"]) == 0

    assert calls["host"] == WILDCARD_HOST
