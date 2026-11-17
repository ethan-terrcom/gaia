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
from typing import Any, Literal, Self

import structlog
from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from gaia.domain.enums import RuntimeKind

logger = structlog.get_logger(__name__)

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

#: The PATH every agent and sandboxed command gets. It includes the Apple-silicon
#: Homebrew prefix and the user uv directory, which the old hard-coded system
#: PATH omitted; entries are expanded and validated by :class:`Settings`.
DEFAULT_AGENT_PATH: tuple[str, ...] = (
    "/opt/homebrew/bin",
    "/usr/local/bin",
    "~/.local/bin",
    "/usr/bin",
    "/bin",
    "/usr/sbin",
    "/sbin",
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


def _derive_log_dir(data: dict[str, Any]) -> Path:
    """Keep JSON logs inside the data directory unless set explicitly."""
    return Path(data["data_dir"]) / "logs"


class Settings(BaseSettings):
    """Gaia's runtime configuration.

    ``db_url``, ``checkpoint_path``, ``worktrees_root`` and ``log_dir`` follow
    ``data_dir`` when they are not supplied, which is why :attr:`data_dir` is
    declared first: a ``default_factory`` only sees the fields validated before
    it.
    """

    model_config = SettingsConfigDict(env_prefix="GAIA_", env_file=None)

    data_dir: Path = Field(default_factory=lambda: DEFAULT_DATA_DIR)
    db_url: str = Field(default_factory=_derive_db_url)
    checkpoint_path: Path = Field(default_factory=_derive_checkpoint_path)
    worktrees_root: Path = Field(default_factory=_derive_worktrees_root)

    # --- logging (18b) ---
    #: Directory for redacted JSON log files (``gaia.jsonl``). Created owner-only
    #: (``0o700``) when ``gaia serve`` starts; derived from ``data_dir`` unless set.
    log_dir: Path = Field(default_factory=_derive_log_dir)
    #: Maximum size of ``gaia.jsonl`` before it rotates to ``gaia.jsonl.1``.
    log_file_max_bytes: int = Field(default=10_000_000, ge=1)
    #: Number of rotated JSON log files to keep, plus the active one.
    log_file_backups: int = Field(default=5, ge=0)

    host: str = "127.0.0.1"
    allow_remote: bool = False
    port: int = 8765
    #: Printed by ``gaia ui`` as the login link. In dev this is Vite's origin;
    #: when :attr:`ui_dist_dir` is set, the API serves the built UI itself, so the
    #: operator should set ``GAIA_UI_ORIGIN`` to the API's own origin
    #: (``http://127.0.0.1:<port>``). The default is deliberately left alone.
    ui_origin: str = "http://127.0.0.1:5173"
    #: Directory containing the built frontend (``frontend/dist``). When set and
    #: it contains an ``index.html``, ``create_app`` serves the SPA and its assets
    #: from this process (doc 16). ``None`` keeps API-only serving.
    ui_dist_dir: Path | None = None

    lead_model: str = "claude-opus-5-5"
    junior_model: str = "deepseek-v4-pro"
    #: Provider and model for the lead's planning and review calls (ADR-001 P2).
    planner_provider: Literal["anthropic", "deepseek"] = "anthropic"
    planner_model: str = "claude-opus-5-5"
    #: How long one lead planning/review LLM call may run before it is timed out.
    lead_call_timeout_s: float = 600
    #: Base URL the gateway forwards Anthropic calls to (doc 07b). Tests and the
    #: doc 08 spike can point this at a local fake without touching real DNS.
    anthropic_base_url: str = "https://api.anthropic.com"
    junior_base_url: str = "https://api.deepseek.com"

    #: Role → runtime/model settings (ADR-001 P2). The orchestrator reads these;
    #: it never names a vendor or a model directly.
    junior_runtime: RuntimeKind = RuntimeKind.NATIVE_DEEPSEEK
    lead_runtime: RuntimeKind = RuntimeKind.CLAUDE_AGENT_SDK
    #: From this attempt on, a step runs on the lead runtime; 0 disables the
    #: lead escalation path so every attempt uses the junior runtime.
    lead_attempt_from: int = Field(default=3, ge=0)
    #: Wall-clock and turn limits for one implementation attempt (doc 12b).
    step_timeout_s: float = Field(default=1800, gt=0)
    junior_max_turns: int = Field(default=60, ge=1)
    lead_max_turns: int = Field(default=60, ge=1)
    #: Timeouts for the owner's setup command and for a step's check command.
    setup_timeout_s: float = Field(default=900, gt=0)
    check_timeout_s: float = Field(default=900, gt=0)
    #: Domains the owner-registered setup command may reach (doc 12b). The check
    #: runner never gets network access.
    setup_allowed_domains: tuple[str, ...] = (
        "pypi.org",
        "files.pythonhosted.org",
        "registry.npmjs.org",
        "github.com",
        "objects.githubusercontent.com",
        # GitHub serves release downloads (uv fetches a managed Python this way) from here.
        "release-assets.githubusercontent.com",
        "crates.io",
        "static.crates.io",
        "proxy.golang.org",
    )

    run_budget_usd: float = Field(default=5.0, gt=0)
    daily_budget_usd: float = Field(default=20.0, gt=0)
    #: How many graph invocations may run at once (doc 12a).
    max_concurrent_runs: int = Field(default=3, ge=1)

    #: Path to the sandbox-runtime ``srt`` binary. ``None`` uses the pinned copy
    #: installed by ``make sandbox-install`` under ``data_dir``.
    srt_path: Path | None = None

    #: Path to ripgrep. ``None`` asks :func:`gaia.tools.fs.find_rg` to look it up
    #: on :attr:`agent_path`; when it is not found there either, the search tool
    #: uses its fixed-string Python fallback.
    rg_path: Path | None = None

    #: The PATH given to agent processes and sandboxed commands. Entries must be
    #: absolute after ``~`` expansion, so a relative entry cannot be resolved
    #: against whatever directory a command happens to start in.
    agent_path: tuple[str, ...] = Field(default_factory=lambda: DEFAULT_AGENT_PATH)

    #: Toolchain directories the sandbox may read (doc 09a). The defaults are
    #: package-manager caches and toolchain homes; entries that do not exist are
    #: ignored when the sandbox settings are built.
    sandbox_toolchain_read: tuple[Path, ...] = Field(
        default_factory=lambda: DEFAULT_SANDBOX_TOOLCHAIN_READ
    )

    #: Where sandboxed commands look for (and would install) uv-managed Pythons.
    #: Sandboxed commands get ``HOME`` set to a throwaway task directory, so
    #: without this uv installs a fresh Python under the *setup* command's task
    #: directory and the worktree's ``.venv`` links into it - a directory no later
    #: command's sandbox can read. Pointing uv at the owner's real install dir,
    #: which ``sandbox_toolchain_read`` already lets the sandbox read, keeps the
    #: venv usable by checks and agents. Used only when it exists and is readable.
    sandbox_uv_python_dir: Path | None = Path("~/.local/share/uv/python")

    #: Register the dev-only fake-run publisher (doc 13 acceptance criterion).
    dev_routes: bool = False

    #: How long ``watch_ci`` sleeps after a poll with no change (doc 15b).
    ci_poll_interval_s: float = Field(default=30.0, gt=0)
    #: Hard deadline for ``watch_ci`` before it reports CI as failed (doc 15b).
    ci_watch_timeout_s: float = Field(default=7200.0, gt=0)
    #: How long ``watch_ci`` keeps polling a repository that reports no CI at all
    #: before recording ``unknown`` and treating the PR as passing (doc 15b).
    ci_no_checks_grace_s: float = Field(default=120.0, ge=0)
    #: Whether the API lifespan builds the orchestrator and starts/recover runs.
    #: Tests that build the app only for unrelated routes set this ``False`` so
    #: they never open a checkpointer file or launch background run tasks.
    orchestrator_enabled: bool = True

    #: How long an agent waits, inside its turn, for a human to decide a command
    #: approval before it counts as rejected (doc 10, requirement 8). The Claude
    #: Agent SDK's own tool-call timeout is derived from it (10c, C3).
    command_approval_timeout_s: float = Field(default=600, ge=30)
    #: Whether the background GitHub PR sync task runs in the API lifespan.
    pr_sync_enabled: bool = True
    #: How often the background PR sync task refreshes GitHub's view of open PRs.
    pr_sync_interval_s: float = Field(default=60.0, ge=5)

    # --- cleanup (18c) ---
    #: Whether the background cleanup task runs in the API lifespan. Off by
    #: default: deleting a user's checkouts on a timer is something they opt into.
    cleanup_enabled: bool = False
    #: Remove worktrees and scratch dirs for runs finished at least this old.
    cleanup_older_than_days: int = Field(default=14, ge=1)
    #: How often the background cleanup task plans and applies a round.
    cleanup_interval_s: float = Field(default=86400.0, gt=0)

    # --- demo mode (18b-2) ---
    #: When true, the LLM gateway answers with the local fake provider and no
    #: real model, provider key or network is used. Read from ``GAIA_FAKE_LLM``.
    fake_llm: bool = False

    @field_validator("data_dir", "checkpoint_path", "worktrees_root", "log_dir")
    @classmethod
    def _expand_path(cls, value: Path) -> Path:
        """Expand ``~`` so the default ``~/.gaia`` is usable as it stands."""
        return value.expanduser()

    @field_validator("srt_path", "rg_path", "ui_dist_dir")
    @classmethod
    def _expand_optional_path(cls, value: Path | None) -> Path | None:
        """Expand ``~`` when the owner supplies a custom executable or directory path."""
        return value.expanduser() if value is not None else None

    @field_validator("agent_path")
    @classmethod
    def _expand_and_check_agent_path(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        """Expand ``~`` and require every PATH entry to be absolute afterwards."""
        expanded = tuple(str(Path(entry).expanduser()) for entry in value)
        for entry in expanded:
            if not Path(entry).is_absolute():
                raise ValueError(f"agent_path entry {entry!r} is not absolute")
        return expanded

    @field_validator("sandbox_toolchain_read")
    @classmethod
    def _expand_toolchain_paths(cls, value: tuple[Path, ...]) -> tuple[Path, ...]:
        """Expand ``~`` in every toolchain path."""
        return tuple(path.expanduser() for path in value)

    @field_validator("sandbox_uv_python_dir")
    @classmethod
    def _expand_uv_python_dir(cls, value: Path | None) -> Path | None:
        """Expand ``~`` once, at startup, like the toolchain paths."""
        return value.expanduser() if value is not None else None

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

    @model_validator(mode="after")
    def _force_native_runtimes_in_demo_mode(self) -> Self:
        """Keep demo mode from launching the Claude Agent SDK.

        The SDK runs the real ``claude`` CLI, which would spend real tokens and
        call a real provider. Demo mode is a local fake, so both roles are
        forced onto the native DeepSeek runtime.
        """
        if not self.fake_llm:
            return self
        if (
            self.lead_runtime is not RuntimeKind.NATIVE_DEEPSEEK
            or self.junior_runtime is not RuntimeKind.NATIVE_DEEPSEEK
        ):
            logger.warning(
                "gaia.demo_mode_runtime_override",
                lead_runtime=self.lead_runtime.value,
                junior_runtime=self.junior_runtime.value,
            )
            self.lead_runtime = RuntimeKind.NATIVE_DEEPSEEK
            self.junior_runtime = RuntimeKind.NATIVE_DEEPSEEK
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
