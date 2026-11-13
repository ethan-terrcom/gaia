# 02 — Config & Secrets

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
  values are rejected; a fetched value is redacted afterwards.
- Redaction masks every pattern above, registered secrets, values nested in dicts and lists, and a
  secret inside a formatted exception. Short values (< 8 characters) are not registered.
- CLI (with `getpass` monkeypatched, because a non-TTY `getpass` warns and warnings are errors):
  `list` never prints values; `set` stores the value and never prints it; `set GAIA_API_TOKEN` is
  refused; `delete` of an unset name exits 1.
- One `@pytest.mark.live` test round-trips a dummy value through `KeyringSecretStore(service="gaia-test")`
  on the real keychain and deletes it.

## Acceptance criteria
- [ ] `uv run gaia secrets set ANTHROPIC_API_KEY` stores the key in the macOS Keychain (verified by the
      owner by hand with a real key; the junior verifies with the live test above).
- [ ] After `gaia secrets list` has run, `grep -r "sk-" ~/.gaia` finds nothing, and `~/.gaia` is mode `0700`.
- [ ] `make check` passes, and no test touches the real keychain (CI on Linux proves it).
