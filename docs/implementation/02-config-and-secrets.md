# 02 — Config & Secrets

**Status:** done (PR `impl/02-config-and-secrets`). The tasks below are the plan as written;
"Deviations as built" records what actually shipped.

## Goal
Typed settings, plus a secrets store backed by the OS keychain, plus log redaction.
No API key ever lives in a plaintext file managed by Gaia.

## Out of scope
Cloud secret managers (leave the `SecretStore` interface for them). Generating `GAIA_API_TOKEN`
(doc 04). JSON log files and rotation (doc 18b). Per-run gateway tokens (doc 07b; they will reuse
`register_secret()`).

## Files
- `gaia/config/settings.py`: `Settings`, `get_settings()`, `ensure_data_dir()`
- `gaia/config/secrets.py`: `SecretName`, `SecretStore`, `KeyringSecretStore`, `InMemorySecretStore`,
  `MissingSecretError`
- `gaia/config/logging.py`: `configure_logging()`. A minimal structlog setup that installs redaction; 18b extends it.
- `gaia/security/redaction.py`: `redact()`, `register_secret()`, the structlog processor. This doc
  owns this file, as an explicit exception to the "09 only" rule for `gaia/security/`.
- `gaia/cli.py`: **argparse** (stdlib, so no new dependency), registered as the `gaia` console script
  under `[project.scripts]` in `backend/pyproject.toml` (`gaia = "gaia.cli:main"`)
- `backend/tests/`: tests, plus a `conftest.py` that keeps every test away from the real keychain

## Tasks
1. `Settings(BaseSettings)` with the env prefix `GAIA_`. **No `env_file`.** Gaia never reads `.env`
   files. Fields and defaults:
   - `data_dir: Path = ~/.gaia`, with `~` expanded
   - `db_url` (default `sqlite+aiosqlite:///{data_dir}/gaia.sqlite`), `checkpoint_path`
     (default `{data_dir}/checkpoints.sqlite`), `worktrees_root` (default `{data_dir}/worktrees`). These are
     derived from `data_dir` in a `model_validator(mode="after")` when not set explicitly.
   - `host = "127.0.0.1"` and `allow_remote = False`. A validator rejects any other host unless
     `allow_remote` is true (`0.0.0.0`, `::`, `localhost` and LAN IPs are all rejected).
   - `port = 8765`, `ui_origin = "http://127.0.0.1:5173"`
   - `lead_model = "claude-opus-5-5"`, `junior_model = "deepseek-v4-pro"` (DeepSeek's current IDs are
     `deepseek-v4-pro` and `deepseek-flash`; `deepseek-chat` is retired; ADR-001 confirms the default),
     `junior_base_url = "https://api.deepseek.com"`
   - `run_budget_usd = 5.0`, `daily_budget_usd = 20.0`, both > 0
   - Settings must **not** contain API keys. A test fails if any field name contains `key`, `token`,
     `secret` or `password`.
   - `get_settings()` returns a cached instance (`functools.lru_cache`).
2. `ensure_data_dir(settings) -> Path` creates `data_dir` and then `chmod`s it to `0o700`, every time.
   `mkdir(mode=…)` is subject to the umask, and an existing directory may have looser permissions.
   The CLI calls it on startup; doc 04's app lifespan will too.
3. Secrets:
   - `SecretName(StrEnum)`: `ANTHROPIC_API_KEY`, `DEEPSEEK_API_KEY`, `GITHUB_TOKEN`, `GAIA_API_TOKEN`.
   - The `SecretStore` protocol: `get(name) -> SecretStr | None`, `require(name) -> SecretStr`,
     `set(name, value: str) -> None`, `delete(name) -> bool` (False if it was not set).
   - `require()` raises `MissingSecretError` with the fix in the message, for example
     `ANTHROPIC_API_KEY is not set. Run: gaia secrets set ANTHROPIC_API_KEY`.
   - `set()` rejects empty values and values containing whitespace (after stripping the ends).
   - `KeyringSecretStore(service: str = "gaia")` uses `keyring` with the enum value as the username.
     `InMemorySecretStore` implements the same protocol for tests and for later docs' tests.
   - Every value a store returns from `get()` or `require()` is passed to `register_secret()`, so it is
     redacted from then on.
4. Redaction (`gaia/security/redaction.py`):
   - `register_secret(value: str)` adds a literal to mask. Ignore values shorter than 8 characters,
     because masking short common substrings would garble logs.
   - `redact(text: str) -> str` replaces every registered secret and every match of these patterns
     with `[REDACTED]`. Check `sk-ant-` before the generic `sk-` pattern:
     `sk-ant-[\w-]+`, `sk-[A-Za-z0-9]{20,}`, `ghp_\w+`, `github_pat_\w+`, and
     `(?i)bearer\s+[\w.~+/=-]{16,}` (masks the token, keeps the word `Bearer`).
     Doc 13 reuses `redact()` for events.
   - `redaction_processor(logger, method_name, event_dict)` is a structlog processor. It redacts every
     `str` value in the event dict, recursively through dicts, lists and tuples.
   - `configure_logging()` places the processor **after** `format_exc_info`, so traceback text is
     redacted too, and immediately before the renderer.
5. CLI (`gaia/cli.py`, `main(argv: list[str] | None = None) -> int`):
   - `gaia secrets set NAME` reads the value with `getpass.getpass()`, never from argv, stdin or env.
     It refuses `GAIA_API_TOKEN`, which Gaia generates (doc 04). It prints `Stored NAME in the keychain.`
     and never echoes the value.
   - `gaia secrets list` prints each name with `set` or `not set`, **never values**.
   - `gaia secrets delete NAME` exits 1 with a clear message if NAME was not set.
   - Unknown names are rejected by argparse `choices`. `gaia --version` prints `gaia.__version__`.
   - The store is obtained through one factory function, so tests can substitute `InMemorySecretStore`.

## Security notes
- In `README.md`, explain how to migrate a key that currently sits in `~/.zshrc`: run
  `gaia secrets set DEEPSEEK_API_KEY`, then delete the export line, open a new shell, and **rotate the
  key**, because it has been in plaintext.
- No test touches the real keychain or `~/.gaia`. `tests/conftest.py` installs an in-memory keyring
  backend for every test (`keyring.set_keyring(...)`; CI runs on Linux, which has no keychain), and tests
  use `tmp_path` for `data_dir`.

## Tests
- Settings: the defaults are derived from `data_dir`; `host="0.0.0.0"` (and `localhost`, `::`) is
  rejected unless `allow_remote=True`; no field name looks like a credential; `GAIA_PORT` in the
  environment is honoured; a `.env` file in the working directory is ignored.
- `ensure_data_dir` gives `0o700` both for a new directory and for an existing `0o755` one.
- Secrets: the in-memory and keyring stores (with the in-memory backend) round-trip `set`, `get` and
  `delete`; `require` on a missing name raises an error naming the CLI command; empty or whitespace
  values are rejected; a fetched value is redacted afterwards, and a deleted one stays redacted.
- Redaction masks every pattern above, registered secrets, values nested in dicts and lists, and a
  secret inside a formatted exception. Short values (< 8 characters) are not registered.
- CLI (with `getpass` monkeypatched, because a non-TTY `getpass` warns and warnings are errors):
  `list` never prints values; `set` stores the value and never prints it; `set GAIA_API_TOKEN` is
  refused; `delete` of an unset name exits 1.
- One `@pytest.mark.live` test round-trips a dummy value through `KeyringSecretStore(service="gaia-test")`
  on the real keychain and deletes it.

## Deviations as built

1. **The derived path fields are non-optional and derived with a data-aware `default_factory`.**
   Task 1 says `model_validator(mode="after")`, and that is what the first draft did - which forces
   `db_url`, `checkpoint_path` and `worktrees_root` to be declared `| None` so that "not set" stays
   distinguishable from an explicit value. The cost lands on every consumer (docs 03, 04, 05, 12): each
   one narrows a value that cannot be `None`, and the tempting `settings.db_url or <fallback>` hides a
   misconfiguration instead of failing. pydantic >= 2.10 passes the already-validated model data to a
   `default_factory` that takes one positional argument, so the fields are declared `str` / `Path`
   with `default_factory=_derive_db_url` and friends, and `_derive_paths` is gone. Two constraints this
   keeps: `data_dir` must stay declared before them (a factory only sees fields validated earlier),
   and an explicit `GAIA_DB_URL` / `GAIA_CHECKPOINT_PATH` / `GAIA_WORKTREES_ROOT` still wins, because a
   factory only runs when nothing was supplied. pydantic-settings sets `validate_default=True`, so the
   derived values still pass through the `~`-expanding field validator. `tests/test_settings.py` pins
   the annotations, the derivation from an expanded `data_dir`, and that explicit values are kept.
2. **`localhost` is rejected as "not an IP address".** The validator parses `host` with
   `ipaddress.ip_address()` and then requires `is_loopback`, so a hostname is refused before it can
   be resolved through `/etc/hosts`. Only `127.0.0.0/8` and `::1` pass without `allow_remote=True`.
   See "Hand-off to doc 04" below: this constrains the **bind** address only.
3. **`ensure_data_dir(settings)` also expands `~`** before `mkdir`/`chmod`, so it is safe to call with
   the raw `~/.gaia` default outside `Settings`. `chmod` runs on every call (an existing directory can
   be looser than the umask allowed).
4. **The secret factory is `gaia.config.secrets.get_secret_store()`.** The CLI resolves the store
   through it on every invocation, which is what the tests patch (`InMemorySecretStore`).
5. **`configure_logging(level=logging.INFO, stream=None)`** takes keyword arguments only and sets
   `cache_logger_on_first_use=False`; a caching config would make the logging tests unreconfigurable.
   `level` accepts a `logging` level, so doc 18b can raise it.
6. **Redaction order:** registered literals are masked first (longest first), then the token patterns.
   `MIN_SECRET_LENGTH = 8`. The registry is module state with `_reset_for_tests()`; `tests/conftest.py`
   clears it around every test. **`delete()` does not un-register the value**: a traceback or a log
   record that captured it may not be rendered yet, the registry is per process and a few strings
   large, and masking a retired value is harmless where printing it is not. The protocol and both
   implementations say so, and `test_deleting_a_secret_does_not_un_redact_it` pins it, so a later
   "cleanup" cannot quietly turn this into a leak.
7. **CLI dispatch** is a `_COMMANDS` table keyed by `args.command`; `secrets` is the only entry today.
   `ensure_data_dir()` runs before dispatch, so every command (including `list` and `delete`) leaves
   `~/.gaia` at `0700`. `secrets set` refuses `GAIA_API_TOKEN` with exit code 1 *before* prompting.
8. **Lint:** three inline suppressions in the whole backend, one per line, each carrying its reason.
   `# noqa: S105` on the two enum members whose *names* contain `TOKEN` (they are keychain entry
   names, never values) and `# noqa: S104` on the one `0.0.0.0` literal in the settings tests (a
   literal address in a test cannot bind a socket). No `[tool.*]` section changed: the only edit to
   `backend/pyproject.toml` is the `[project.scripts]` entry that task 5 requires.
   `[tool.ruff.lint.per-file-ignores]` already waives `S101`/`S105`/`S106`/`S108`/`S603`/`S607`/`TID251`
   for `tests/**`, so adding `S104` there was the alternative; the owner chose the inline form in
   review, and a module-wide `S105` ignore in `gaia/config/secrets.py` was rejected outright because
   that module stores credentials.
9. **Tests:** `tests/conftest.py` installs an in-memory `keyring` backend **and** deletes inherited
   `GAIA_*` variables, so a developer's `GAIA_DATA_DIR` cannot point a test at the real `~/.gaia`.
   The real-keychain round trip is `@pytest.mark.live` (deselected by default; `uv run pytest -m live`).
10. **`README.md`** gained a "Secrets" section: the CLI commands (written as
    `uv run --project backend gaia …`, because the repo root has no Python project and plain
    `uv run gaia …` there fails with `Failed to spawn: gaia`), which key each feature needs, the
    `~/.zshrc` migration, and the instruction to rotate a key that was stored in plaintext.

## Hand-off to doc 04 (host binding)

The bind address and the `Host` header are different things, so doc 04 task 3 does not conflict with
this package:

- `settings.host` is the **bind** address and must be an IP literal. Doc 04 task 6 passes it straight
  to uvicorn, so `uvicorn(host=settings.host)` never resolves a name and never depends on
  `/etc/hosts`. `::1` is accepted as well as `127.0.0.1`; binding `::1` serves IPv6 clients only, so
  `127.0.0.1` stays the default.
- `TrustedHostMiddleware(allowed_hosts=["127.0.0.1", "localhost"])` stays exactly as doc 04 writes it:
  that list constrains the `Host` **header a client sends**, not the socket. Dropping `localhost`
  would make `http://localhost:8765` answer 400, and a browser that resolves `localhost` to `::1`
  would fail to connect at all - `ui_origin`, `frontend/vite.config.ts` and `gaia ui` all use
  `127.0.0.1`, which is the address users actually reach.
- `allow_remote` is all-or-nothing: when it is true the loopback check is skipped entirely, so
  `GAIA_ALLOW_REMOTE=true` with `GAIA_HOST=localhost` (or `0.0.0.0`, or any hostname) binds. That is
  the deliberate opt-in for a container or LAN setup; the strict check stays the default.

## Acceptance criteria
- [ ] `uv run gaia secrets set ANTHROPIC_API_KEY` stores the key in the macOS Keychain (verified by the
      owner by hand with a real key; the junior verifies with the live test above).
      Run it from `backend/`, or as `uv run --project backend gaia secrets set ANTHROPIC_API_KEY` from the
      repo root: `gaia` is a console script of the backend project, so `uv run` at the root reports
      `Failed to spawn: gaia`.
      **Status:** the live test passes on macOS (`uv run pytest -m live`), which round-trips a dummy value
      through the real Keychain; the real key is for the owner to store.
- [x] After `gaia secrets list` has run, `grep -r "sk-" ~/.gaia` finds nothing, and `~/.gaia` is mode `0700`.
      Verified by `tests/test_cli.py` (the CLI creates `data_dir` at `0700` and `list` prints names only, no
      values) and by `tests/test_settings.py::test_ensure_data_dir_*`.
- [x] `make check` passes, and no test touches the real keychain (CI on Linux proves it). `tests/conftest.py`
      installs an in-memory `keyring` backend for every test; the single real-keychain test is `live`-marked
      and deselected by default. `make check` passes locally: 113 tests, 100% backend coverage, gitleaks
      clean over the full history.
