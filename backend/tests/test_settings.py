"""Tests for ``gaia.config.settings`` (doc 02, tasks 1 and 2)."""

from __future__ import annotations

import stat
from pathlib import Path
from typing import get_args

import pytest
from pydantic import ValidationError

from gaia.config.settings import Settings, ensure_data_dir, get_settings

#: Nothing in Settings may look like a credential: keys live in the keychain.
CREDENTIAL_WORDS = ("key", "token", "secret", "password")

#: Fields that follow ``data_dir`` when the operator does not supply them.
DERIVED_FIELDS = ("db_url", "checkpoint_path", "worktrees_root")

#: Named once so the S104 suppression has a single home: a literal address in a test
#: never binds a socket.
ALL_INTERFACES = "0.0.0.0"  # noqa: S104


def test_defaults_are_derived_from_data_dir(tmp_path: Path) -> None:
    data_dir = tmp_path / "gaia-data"

    settings = Settings(data_dir=data_dir)

    assert settings.data_dir == data_dir
    assert settings.db_url == f"sqlite+aiosqlite:///{data_dir}/gaia.sqlite"
    assert settings.checkpoint_path == data_dir / "checkpoints.sqlite"
    assert settings.worktrees_root == data_dir / "worktrees"


def test_explicit_paths_are_not_overwritten(tmp_path: Path) -> None:
    settings = Settings(
        data_dir=tmp_path,
        db_url="postgresql+asyncpg://localhost/gaia",
        checkpoint_path=tmp_path / "elsewhere" / "checkpoints.sqlite",
        worktrees_root=tmp_path / "elsewhere" / "worktrees",
    )

    assert settings.db_url == "postgresql+asyncpg://localhost/gaia"
    assert settings.checkpoint_path == tmp_path / "elsewhere" / "checkpoints.sqlite"
    assert settings.worktrees_root == tmp_path / "elsewhere" / "worktrees"


def test_derived_paths_are_not_optional() -> None:
    """They are always populated, so a ``| None`` here would cost every caller.

    Docs 03, 04, 05 and 12 read these fields; an optional annotation would make
    each of them narrow a value that cannot be ``None``, and the tempting
    ``settings.db_url or <fallback>`` would hide a misconfiguration instead of
    failing.
    """
    for name in DERIVED_FIELDS:
        annotation = Settings.model_fields[name].annotation
        assert type(None) not in get_args(annotation), f"{name} is always populated"


def test_derived_paths_keep_their_declared_types() -> None:
    assert Settings.model_fields["db_url"].annotation is str
    assert Settings.model_fields["checkpoint_path"].annotation is Path
    assert Settings.model_fields["worktrees_root"].annotation is Path


def test_default_data_dir_expands_the_home_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))

    settings = Settings()

    assert settings.data_dir == tmp_path / ".gaia"
    # Derived from the *expanded* directory, not from the raw ``~/.gaia``.
    assert settings.checkpoint_path == tmp_path / ".gaia" / "checkpoints.sqlite"


def test_data_dir_from_the_environment_expands_the_home_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("GAIA_DATA_DIR", "~/gaia-test")

    settings = Settings()

    assert settings.data_dir == tmp_path / "gaia-test"
    assert settings.db_url == f"sqlite+aiosqlite:///{tmp_path / 'gaia-test'}/gaia.sqlite"
    assert settings.worktrees_root == tmp_path / "gaia-test" / "worktrees"


def test_default_host_and_port_are_local() -> None:
    settings = Settings()

    assert settings.host == "127.0.0.1"
    assert settings.port == 8765
    assert settings.allow_remote is False
    assert settings.ui_origin == "http://127.0.0.1:5173"


def test_models_match_the_architecture_document() -> None:
    settings = Settings()

    assert settings.lead_model == "claude-opus-5-5"
    assert settings.junior_model == "deepseek-v4-pro"
    assert settings.junior_base_url == "https://api.deepseek.com"


def test_default_budgets_are_positive() -> None:
    settings = Settings()

    assert settings.run_budget_usd == 5.0
    assert settings.daily_budget_usd == 20.0


def test_srt_path_defaults_to_none() -> None:
    assert Settings().srt_path is None


def test_srt_path_expands_the_home_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))

    settings = Settings(srt_path=Path("~/bin/srt"))

    assert settings.srt_path == tmp_path / "bin" / "srt"


def test_sandbox_toolchain_read_defaults_expand_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))

    settings = Settings()

    assert settings.sandbox_toolchain_read[0] == tmp_path / ".cache" / "uv"
    assert settings.sandbox_toolchain_read[-1] == tmp_path / ".pyenv"


def test_sandbox_toolchain_read_can_be_overridden(tmp_path: Path) -> None:
    toolchain = (tmp_path / "one", tmp_path / "two")

    settings = Settings(sandbox_toolchain_read=toolchain)

    assert settings.sandbox_toolchain_read == toolchain


@pytest.mark.parametrize(
    "host",
    [ALL_INTERFACES, "::", "localhost", "192.168.1.10", "10.0.0.5", "example.com", ""],
)
def test_a_non_loopback_host_is_rejected(host: str) -> None:
    with pytest.raises(ValidationError, match="host"):
        Settings(host=host)


def test_a_loopback_host_is_allowed() -> None:
    assert Settings(host="127.0.0.1").host == "127.0.0.1"
    assert Settings(host="::1").host == "::1"


def test_another_host_needs_an_explicit_opt_in() -> None:
    settings = Settings(host=ALL_INTERFACES, allow_remote=True)

    assert settings.host == ALL_INTERFACES


@pytest.mark.parametrize("field", ["run_budget_usd", "daily_budget_usd"])
@pytest.mark.parametrize("value", [0.0, -1.0])
def test_budgets_must_be_positive(field: str, value: float, tmp_path: Path) -> None:
    with pytest.raises(ValidationError):
        Settings.model_validate({"data_dir": tmp_path, field: value})


def test_no_field_name_looks_like_a_credential() -> None:
    for field_name in Settings.model_fields:
        offending = [word for word in CREDENTIAL_WORDS if word in field_name.lower()]
        assert not offending, f"{field_name} looks like a credential; keys belong in the keychain"


def test_gaia_environment_variables_are_honoured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GAIA_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("GAIA_PORT", "9999")
    monkeypatch.setenv("GAIA_RUN_BUDGET_USD", "1.25")

    settings = Settings()

    assert settings.port == 9999
    assert settings.data_dir == tmp_path
    assert settings.run_budget_usd == 1.25


def test_a_dotenv_file_in_the_working_directory_is_ignored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / ".env").write_text("GAIA_PORT=1234\nGAIA_HOST=0.0.0.0\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    settings = Settings(data_dir=tmp_path / "data")

    assert settings.port == 8765
    assert settings.host == "127.0.0.1"


def test_get_settings_is_cached(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GAIA_DATA_DIR", str(tmp_path))

    first = get_settings()

    assert get_settings() is first
    monkeypatch.setenv("GAIA_PORT", "9999")
    assert get_settings() is first


def test_ensure_data_dir_creates_a_private_directory(tmp_path: Path) -> None:
    data_dir = tmp_path / "gaia-data"

    returned = ensure_data_dir(Settings(data_dir=data_dir))

    assert returned == data_dir
    assert data_dir.is_dir()
    assert stat.S_IMODE(data_dir.stat().st_mode) == 0o700


def test_ensure_data_dir_tightens_an_existing_loose_directory(tmp_path: Path) -> None:
    data_dir = tmp_path / "gaia-data"
    data_dir.mkdir()
    data_dir.chmod(0o755)

    ensure_data_dir(Settings(data_dir=data_dir))

    assert stat.S_IMODE(data_dir.stat().st_mode) == 0o700


def test_ensure_data_dir_expands_the_home_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))

    returned = ensure_data_dir(Settings(data_dir=Path("~/.gaia")))

    assert returned == tmp_path / ".gaia"
    assert stat.S_IMODE(returned.stat().st_mode) == 0o700
