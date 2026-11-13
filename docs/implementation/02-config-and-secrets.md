# 02 — Config & Secrets

## Goal
Typed settings, plus a secrets store backed by the OS keychain, plus log redaction.
No API key ever lives in a plaintext file managed by Gaia.

## Out of scope
Cloud secret managers. Leave a `SecretStore` interface for them.

## Files
- `gaia/config/settings.py`
- `gaia/config/secrets.py`
- `gaia/security/redaction.py`
- `gaia/cli.py` (Typer, or argparse to avoid a new dependency)

## Tasks
1. `Settings(BaseSettings)` with the env prefix `GAIA_`. Fields: `data_dir` (default `~/.gaia`),
   `db_url` (default `sqlite+aiosqlite:///{data_dir}/gaia.sqlite`), `checkpoint_path`,
   `host` (default and **validated** to be `127.0.0.1` unless `allow_remote=True`), `port`,
   `ui_origin` (default `http://127.0.0.1:5173`), `worktrees_root` (default `{data_dir}/worktrees`),
   `lead_model="claude-opus-5-5"`, `junior_model="deepseek-chat"` (confirm the current DeepSeek
   model ids against their docs), `daily_budget_usd`, `run_budget_usd`.
   Settings must **not** contain API keys.
2. `SecretStore` protocol with `get(name) -> SecretStr | None`, `set(name, value)` and `delete(name)`.
   `KeyringSecretStore` uses service name `gaia`. Known names live in an enum:
   `ANTHROPIC_API_KEY`, `DEEPSEEK_API_KEY`, `GITHUB_TOKEN`, `GAIA_API_TOKEN`.
3. CLI commands: `gaia secrets set <NAME>` (reads with `getpass`, never from argv), `gaia secrets list`
   (names and whether each is set, **never values**), and `gaia secrets delete <NAME>`.
4. A redaction processor for structlog. It masks any value that matches a loaded secret, plus
   regexes for `sk-ant-[\w-]+`, `sk-[A-Za-z0-9]{20,}`, `ghp_\w+`, `github_pat_\w+`. Export a
   `redact(text) -> str` helper; doc 13 reuses it for events.
5. Create `data_dir` with mode `0o700` on startup.

## Security notes
- Explain in `README.md` how to migrate a key that currently sits in `~/.zshrc`: run
  `gaia secrets set DEEPSEEK_API_KEY`, then delete the export line and rotate the key.
- A missing secret raises a clear error naming the CLI command to fix it.

## Tests
- Settings rejects `host="0.0.0.0"` unless `allow_remote` is set.
- Redaction masks every pattern above as well as dynamically registered secrets.
- `secrets list` never prints values (use a fake in-memory `SecretStore`).

## Acceptance criteria
- [ ] `gaia secrets set ANTHROPIC_API_KEY` stores the key in the macOS Keychain.
- [ ] `grep -r "sk-" ~/.gaia` finds nothing after startup.
