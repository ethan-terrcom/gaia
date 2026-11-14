"""Typed application settings, read from ``GAIA_*`` environment variables.

Owned by doc 02. Two rules shape this module:

- API keys are **not** settings. They live in the OS keychain behind
  :mod:`gaia.config.secrets`, and a test fails if a field name looks like a
  credential.
- Gaia never reads ``.env`` files. There is deliberately no ``env_file``: a
  plaintext dotfile next to the repo is exactly what doc 02 removes.
"""

from __future__ import annotations

import functools
import ipaddress
from pathlib import Path
from typing import Any, Self

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

DEFAULT_DATA_DIR = Path("~/.gaia")

#: Permissions enforced on the data directory: owner read/write/execute only.
DATA_DIR_MODE = 0o700

#: Toolchain directories the sandbox may read, in addition to the worktree and
#: the per-task temp dir. ``build_srt_settings`` keeps only the ones that exist.
DEFAULT_SANDBOX_TOOLCHAIN_READ: tuple[Path, ...] = (
    Path("~/.cache/uv"),
    Path("~/.local/share/uv"),
    Path("~/.local/bin"),
    Path("~/.cargo"),
    Path("~/.rustup"),
    Path("~/.npm"),
    Path("~/.nvm"),
    Path("~/.pyenv"),
)

# The three helpers below derive a path from ``data_dir``. pydantic hands a
# ``default_factory`` that takes one positional argument the already-validated
# model data (signature inspection, see pydantic's ``takes_validated_data_argument``),
# so these stay ordinary non-optional fields instead of ``X | None`` that every
# caller would have to narrow. pydantic-settings also sets ``validate_default``,
# so the derived value still passes through the field validators.


def _derive_db_url(data: dict[str, Any]) -> str:
    """Point the SQLite URL at :attr:`Settings.data_dir` unless set explicitly."""
    return f"sqlite+aiosqlite:///{data['data_dir']}/gaia.sqlite"


def _derive_checkpoint_path(data: dict[str, Any]) -> Path:
    """Keep LangGraph checkpoints next to the database unless set explicitly."""
    return Path(data["data_dir"]) / "checkpoints.sqlite"


def _derive_worktrees_root(data: dict[str, Any]) -> Path:
    """Keep git worktrees inside the data directory unless set explicitly."""
    return Path(data["data_dir"]) / "worktrees"


class Settings(BaseSettings):
    """Gaia's runtime configuration.

    ``db_url``, ``checkpoint_path`` and ``worktrees_root`` follow ``data_dir``
    when they are not supplied, which is why :attr:`data_dir` is declared first:
    a ``default_factory`` only sees the fields validated before it.
    """

    model_config = SettingsConfigDict(env_prefix="GAIA_", env_file=None)

    data_dir: Path = Field(default_factory=lambda: DEFAULT_DATA_DIR)
    db_url: str = Field(default_factory=_derive_db_url)
    checkpoint_path: Path = Field(default_factory=_derive_checkpoint_path)
    worktrees_root: Path = Field(default_factory=_derive_worktrees_root)

    host: str = "127.0.0.1"
    allow_remote: bool = False
    port: int = 8765
    ui_origin: str = "http://127.0.0.1:5173"

    lead_model: str = "claude-opus-5-5"
    junior_model: str = "deepseek-v4-pro"
    junior_base_url: str = "https://api.deepseek.com"

    run_budget_usd: float = Field(default=5.0, gt=0)
    daily_budget_usd: float = Field(default=20.0, gt=0)

    #: Path to the sandbox-runtime ``srt`` binary. ``None`` uses the pinned copy
    #: installed by ``make sandbox-install`` under ``data_dir``.
    srt_path: Path | None = None

    #: Toolchain directories the sandbox may read (doc 09a). The defaults are
    #: package-manager caches and toolchain homes; entries that do not exist are
    #: ignored when the sandbox settings are built.
    sandbox_toolchain_read: tuple[Path, ...] = Field(
        default_factory=lambda: DEFAULT_SANDBOX_TOOLCHAIN_READ
    )

    #: Register the dev-only fake-run publisher (doc 13 acceptance criterion).
    dev_routes: bool = False

    @field_validator("data_dir", "checkpoint_path", "worktrees_root")
    @classmethod
    def _expand_path(cls, value: Path) -> Path:
        """Expand ``~`` so the default ``~/.gaia`` is usable as it stands."""
        return value.expanduser()

    @field_validator("srt_path")
    @classmethod
    def _expand_optional_path(cls, value: Path | None) -> Path | None:
        """Expand ``~`` when the owner supplies a custom ``srt`` path."""
        return value.expanduser() if value is not None else None

    @field_validator("sandbox_toolchain_read")
    @classmethod
    def _expand_toolchain_paths(cls, value: tuple[Path, ...]) -> tuple[Path, ...]:
        """Expand ``~`` in every toolchain path."""
        return tuple(path.expanduser() for path in value)

    @model_validator(mode="after")
    def _check_host_is_local(self) -> Self:
        """Refuse to bind anywhere but loopback unless the owner opts in.

        Nothing in v1 assumes "localhost means trusted" (ARCHITECTURE section 7),
        so the default is a numeric loopback address. ``localhost`` itself is
        rejected on purpose: it can resolve anywhere, including via ``/etc/hosts``.
        """
        if self.allow_remote:
            return self
        if self.host == "":
            raise ValueError("host must be set to a loopback address such as 127.0.0.1")
        try:
            address = ipaddress.ip_address(self.host)
        except ValueError:
            raise ValueError(
                f"host {self.host!r} is not an IP address; use 127.0.0.1 "
                "or set allow_remote=True to bind to another address"
            ) from None
        if not address.is_loopback:
            raise ValueError(
                f"host {self.host!r} is not a loopback address; set "
                "allow_remote=True to bind to another address"
            )
        return self


@functools.lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide settings, built once from the environment."""
    return Settings()


def ensure_data_dir(settings: Settings) -> Path:
    """Create :attr:`Settings.data_dir` if needed and force it to ``0o700``.

    ``mkdir(mode=...)`` is filtered through the umask and leaves an existing
    directory alone, so the ``chmod`` runs every time.
    """
    data_dir = settings.data_dir.expanduser()
    data_dir.mkdir(mode=DATA_DIR_MODE, parents=True, exist_ok=True)
    data_dir.chmod(DATA_DIR_MODE)
    return data_dir
