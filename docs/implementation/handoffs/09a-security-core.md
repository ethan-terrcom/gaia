# Handoff: 09a — Path guard, command policy, sandbox profile (D1)

Filled from `HANDOFF_TEMPLATE.md` for the lead's sandboxed DeepSeek harness (2026-11-14). File tools
are confined to the worktree. The `run` tool is sandboxed: no network, no git writes, no installs. The
lead does the commit and merge, and the report goes through `finish`. **This package is
security-critical and gets architect review:** write it so that a reviewer can check every rule
against a test.

---

You are a junior engineer on **Gaia**, a local-first control plane for AI coding agents. Your task is
to implement exactly one work package, **09a**: tasks 1, 2, 5 and 6 of
`docs/implementation/09-sandboxed-tools-and-command-policy.md`. The tools, registry and audit (tasks 3
and 4) are 09b; do not build them. The lead engineer reviews your work, then commits and merges it.
You never commit.

## 1. Read first, in this order
1. `docs/implementation/09-sandboxed-tools-and-command-policy.md`: your contract.
2. `docs/implementation/00-README.md`, "Rules for every work package".
3. `docs/ARCHITECTURE.md` §6–§7, and `docs/reviews/2026-11-12-plan-review.md` (D1 and D3).
4. `docs/reviews/2026-11-13-code-review-01-05.md`, findings F1 and F2: the class of bug this module
   exists to stop.
5. `docs/backend/README.md`, then `docs/backend/git-and-processes.md`, `config-and-secrets.md` and
   `python-concepts.md`.
6. The code you build on: `gaia/security/redaction.py`, `gaia/services/{proc,git}.py`,
   `gaia/config/settings.py`, and `tests/test_proc.py` for the testing style.

## 2. Lead notes for this package
These are design decisions already made. Follow them, and record any deviation.

**`gaia/security/paths.py`**
- `confine(root: Path, user_path: str) -> Path`, implemented exactly as task 1 describes. Raise
  `PathDenied(UnsafePath)` (subclass the existing `UnsafePath` from `gaia/services/git.py`, or move
  `UnsafePath` into `gaia/security/paths.py` and re-export it from `git.py`; your choice, say which)
  with a reason that never echoes more than 200 characters of the input.
- Reject these inputs:
  - an empty string;
  - NUL bytes;
  - an absolute path, a leading `~`, or a Windows drive or UNC prefix (`C:`, `\\`);
  - any path component equal to `.git`, compared case-insensitively, because APFS is
    case-insensitive (so `.GIT/config` must be denied);
  - a result that is not `is_relative_to(root.resolve())`;
  - an **existing** path any of whose components is a symlink resolving outside the root. Checking
    the final target alone is not enough.
- `root` itself must be an existing directory; otherwise raise.
- `classify_write(root, path) -> Decision`, which is task 5. It returns `ask` for:
  - `.github/workflows/**`, `**/*.lock`, `**/package-lock.json`, `**/pnpm-lock.yaml`,
    `**/yarn.lock`, `**/uv.lock`, `**/poetry.lock`;
  - `.env` and `.env.*` at any depth;
  - `.gaia/**`;
  - the Gaia-adjacent harness config dirs `.claude/**`, `.cline/**`, `.clinerules`, `.mcp.json`,
    `.vscode/**` and `.idea/**`. They change how *other* tools behave in this repo.

  Everything else gets `allow`. Match case-insensitively on the confined, relative POSIX path.

**`gaia/security/commands.py`**
- `Decision` is a frozen dataclass with `action: Literal["allow","deny","ask"]` and `reason: str`.
  Share it with `paths.py`; put it in `gaia/security/decision.py` if that is cleaner.
- `CommandPolicy` is loaded from TOML (`backend/gaia/security/command_policy.default.toml`) through
  Pydantic models with `extra="forbid"`. A malformed file raises at load time: **fail closed**.
- `evaluate(argv: Sequence[str], *, extra_allow: Sequence[Sequence[str]] = ()) -> Decision`. Rules,
  in order:
  1. Validate `argv`: a non-empty list of `str`, at most 256 items, each at most 4096 characters,
     with no NUL and no newline in `argv[0]`. Anything else is `deny`.
  2. If `argv[0]` contains a `/`, it **never matches an allow rule**, because `./pytest` could be a
     script the agent wrote. Evaluate deny rules against its basename; otherwise the result is `ask`.
  3. Unwrap known wrappers, then evaluate the inner command:
     - `uv run [uv-run-flags] <cmd> ...`, where only `--frozen`, `--offline`, `--no-sync` and
       `--quiet`/`-q` may come before `<cmd>`; any other flag gets `ask`;
     - `python -m <module> ...` and `python3 -m ...`, where the module name becomes the command
       (`python -m pytest` = `pytest`);
     - `npx --no-install <cmd>`.

     Any other `uv`, `npm`, `npx` or `pip` usage gets `ask`, because it can install or reach the
     network.
  4. **Deny rules win over everything**, including `extra_allow`. The default deny list:
     - privilege: `sudo`, `su`, `doas`;
     - network: `curl`, `wget`, `ssh`, `scp`, `sftp`, `rsync`, `nc`, `ncat`, `netcat`, `telnet`,
       `ftp`, `socat`;
     - destructive: `rm`, `rmdir`, `dd`, `mkfs`, `shred`, `diskutil`;
     - macOS escapes: `open`, `osascript`, `security`, `launchctl`, `defaults`, `pbcopy`, `pbpaste`,
       `screencapture`;
     - persistence: `crontab`, `at`;
     - ownership: `chown`, `chmod` with `+s` or `u+s`;
     - process control: `kill`, `pkill`, `killall`.

     For `git`, deny the **whole command**, with the reason "use the git tools (09b), which run
     through GitService's hardened environment". This is a deliberate tightening of the doc's example
     policy: running `git` directly would skip `HARDENED_CONFIG` and the `WorktreeLocation` pin from
     05b. Record it as a deviation.
  5. **Shells and interpreters** get `ask`, never `allow`, whatever their arguments: `bash`, `sh`,
     `zsh`, `fish`, `dash`, `ksh`, `env`, `xargs`, `exec`, `eval`, `nohup`, `timeout`, `nice`,
     `time`, `python script.py`, `python -c`, `node -e`, `perl`, `ruby`, `php`, `deno`, `bun`. The
     doc's test `bash -c "..."` → ask must hold.
  6. Allow rules come from the TOML: `pytest`, `ruff`, `mypy`, `black`, `isort`, `eslint`,
     `prettier`, `tsc`, `vitest`, `jest`, `cargo test`, `go test`, `ls`, `cat`, `head`, `tail`,
     `wc`, `grep`, `rg`, `find` (without `-exec`, `-execdir`, `-delete` or `-ok`, which get `ask`),
     `diff`, `echo`, `pwd`, `true`, `false`. An allow rule may restrict subcommands (`cargo` →
     `["test","check","build","fmt","clippy"]`) and may list `deny_flags`.
  7. `extra_allow` entries come from the DB (D3: the owner-registered `test_command` of a repo).
     They match when `argv` **starts with** the entry element by element, compared exactly after the
     wrapper unwrap in step 3. They only upgrade `ask` to `allow`, never `deny` to `allow`.
  8. Anything else gets `ask`.
- The `reason` names the rule that decided, for example `deny: network tool 'curl'` or
  `allow: rule pytest`, so the audit log (09b) explains itself.

**`gaia/security/sandbox.py`** (task 6, D1)
- The sandbox is Anthropic's `sandbox-runtime` (`srt`), pinned to **`@anthropic-ai/sandbox-runtime@0.0.81`**.
  The lead read its README for that version; the relevant facts are below. You have no network, so
  rely on these.
  - The CLI is `srt --settings <file.json> <command...>`. Pass `--` before the command.
  - On macOS, srt shell-quotes the argv into one string and runs it under `sandbox-exec`.
  - The settings file must exist and validate, or srt exits non-zero. It never falls back to
    defaults, which is good: it fails closed.
  - Schema: `{"network": {"allowedDomains": [...], "deniedDomains": [...], "allowLocalBinding": false,
    "allowUnixSockets": []}, "filesystem": {"denyRead": [...], "allowRead": [...],
    "allowWrite": [...], "denyWrite": [...]}, "allowPty": false, "allowAppleEvents": false,
    "enableWeakerNestedSandbox": false, "enableWeakerNetworkIsolation": false}`.
  - **Precedence:** `allowRead` overrides `denyRead`, but `denyWrite` overrides `allowWrite`.
  - Writes are denied by default. Reads are allowed by default. Network is denied by default.
  - A hostname allowed by name is refused if it resolves to loopback. An **IP literal** entry such as
    `127.0.0.1:8765` is allowed as written, so the gateway must be listed as an IP literal with its
    port.
  - `allowAppleEvents: true` would let a command `open` apps outside the sandbox. It must never be
    true.
- `SandboxSpec` is a frozen Pydantic model:
  - `worktree: Path`, plus a per-task `tmp_dir: Path` that the runtime uses as `HOME` and `TMPDIR`;
  - `home: Path`, defaulting to `Path.home()` and injectable for tests;
  - `allowed_domains: tuple[str, ...]`, the repo allowlist from the DB, empty by default;
  - `gateway: str | None`, for example `"127.0.0.1:8765"`;
  - `toolchain_read: tuple[Path, ...]` and `extra_write: tuple[Path, ...]`.
- `build_srt_settings(spec) -> dict[str, Any]`, a pure function with this policy:
  - **Reads:** `denyRead` the whole `home`, then `allowRead` the worktree, the `tmp_dir` and each
    `toolchain_read` path. The defaults come from `Settings.sandbox_toolchain_read`:
    `~/.cache/uv`, `~/.local/share/uv`, `~/.local/bin`, `~/.cargo`, `~/.rustup`, `~/.npm`, `~/.nvm`
    and `~/.pyenv`, keeping only the ones that exist.
  - **Validation**, raising `SandboxConfigError`, which must fail closed:
    - no `allowRead` or `allowWrite` entry may be `home` itself, an ancestor of `home`, `/`, or an
      ancestor of or equal to any sensitive path (`~/.ssh`, `~/.aws`, `~/.config`, `~/.gnupg`,
      `~/Library/Keychains`, `~/.gaia`, `~/.claude`, `~/.cline`, `~/.netrc`, `~/.git-credentials`,
      `~/.docker`, `~/.kube`, `~/.npmrc`, `~/.pypirc`, and the shell profiles);
    - the worktree may live under `~/.gaia/worktrees/`; allow that one directory, never `~/.gaia`.
  - Also list the sensitive paths in `denyRead` explicitly, as defence in depth.
  - **Writes:** `allowWrite` the worktree, `tmp_dir` and `extra_write` only. `denyWrite`:
    - `{worktree}/.git` as `{"path": ..., "literal": true}`;
    - `{worktree}/.github/workflows`, `{worktree}/.gaia`, `{worktree}/.claude` and
      `{worktree}/.cline`;
    - `{worktree}/**/.env` and `{worktree}/**/.env.*`.
  - **Network:** `allowedDomains` is `spec.allowed_domains` plus `spec.gateway`. Validate each entry:
    no scheme, no path, no whitespace, no bare `*`. `deniedDomains` is `[]`. `allowLocalBinding` and
    `allowUnixSockets` are off.
  - Every escape hatch (`allowPty`, `allowAppleEvents`, `enableWeakerNestedSandbox`,
    `enableWeakerNetworkIsolation`) is explicitly `false`.
  - Emit absolute paths only, never `~`, because srt expands `~` with its own `HOME`.
- `write_srt_settings(spec, directory) -> Path` writes the JSON with mode `0o600`.
  `wrap_argv(srt_path, settings_path, argv) -> list[str]` returns
  `[str(srt_path), "--settings", str(settings_path), "--", *argv]`.
- `find_srt(settings) -> Path` checks `Settings.srt_path` (new field, `Path | None = None`), then
  `{data_dir}/tools/node_modules/.bin/srt`. It does **not** fall back to `PATH`, because a `PATH`
  lookup could pick up an unpinned copy. If neither exists, raise `SandboxUnavailable` with a message
  naming `make sandbox-install`.
- Makefile target `sandbox-install`:
  `npm install --prefix "$${GAIA_DATA_DIR:-$$HOME/.gaia}/tools" --save-exact --ignore-scripts @anthropic-ai/sandbox-runtime@0.0.81`.
  Add only that target and its `.PHONY` entry.
- **The doc's sandbox test** ("a `conftest.py` that reads `~/.ssh` and calls `curl` fails under an
  allowlisted `pytest`") needs a real srt, and you cannot run one: your own commands already run
  inside `sandbox-exec`, and macOS refuses to nest it. Write it as `tests/test_sandbox_live.py`:
  - Skip it with `pytest.mark.skipif` unless the `GAIA_SRT_PATH` env var points at an executable.
  - Use a fake `home` under `tmp_path` holding `.ssh/id_test`, and a worktree under `tmp_path`.
  - Write a small throwaway Python project whose `conftest.py` tries to read the fake key and to open
    a TCP connection to `1.1.1.1:443`, and whose test asserts both failed.
  - Run it through `run_proc(wrap_argv(...))`.
  - Add a second case proving a write outside the worktree fails, and a third proving that a write to
    `{worktree}/.git` fails.

  The lead runs this file. Everything else must reach 100% coverage without it.

## 3. What you may and must not touch
**You may create or edit only:**
- `backend/gaia/security/{paths,commands,sandbox,decision}.py` and
  `backend/gaia/security/command_policy.default.toml`;
- `backend/gaia/services/git.py`, only to move or re-export `UnsafePath`;
- `backend/gaia/config/settings.py`, adding the fields `srt_path` and `sandbox_toolchain_read`;
- the `sandbox-install` target in `Makefile`;
- `backend/tests/**`;
- a "Decisions and deviations (as built)" section at the end of
  `docs/implementation/09-sandboxed-tools-and-command-policy.md`.

**You must not:**
- edit other docs, `.github/`, `.pre-commit-config.yaml`, `[tool.*]` in `pyproject.toml` or `uv.lock`;
- add dependencies. `tomllib` is stdlib; no `pathspec` either, so write the small glob matcher you
  need and test it;
- build tools, the registry or audit rows (09b);
- run anything with `shell=True`, or spawn processes outside `run_proc`.

## 4. Non-negotiable rules
- **Fail closed** everywhere: an unknown or invalid input means `deny` or an exception, never `allow`.
- **Table-driven tests** with one row per rule, and a reason string checked on each. Cover the
  doc's cases: `../x`, `/etc/passwd`, a symlink to outside, `a/../../x`, `.git/config` and
  `foo\0bar`. Then add your own: `.GIT/config`, a symlinked parent directory, a dangling symlink,
  `~root`, `C:\x`, a trailing `/.`, a unicode lookalike that is *not* `.git` (allowed), `git push`,
  `/usr/bin/git status` (deny), `./pytest` (ask), `uv run pytest -k x` (allow), `uv run --with x
  pytest` (ask), `python -m pytest` (allow), `python script.py` (ask), `bash -c "..."` (ask),
  `find . -delete` (ask), `chmod u+s x` (deny), and `extra_allow` that cannot override a deny.
- **Library APIs:** use stdlib only (`tomllib`, `pathlib`, `fnmatch` if it fits, `shlex` only for
  messages).

## 5. How to work
1. Plan first: the public API with signatures, then the rule table, then the test table.
2. Build `paths.py`, then `commands.py`, then `sandbox.py`, each with its tests before moving on.
3. Before you finish, all of these must pass, run from `backend/`: `uv run ruff check .`,
   `uv run ruff format --check .`, `uv run mypy` and `uv run pytest --cov -q` (100% coverage of
   `gaia/`).
4. Write the "Decisions and deviations (as built)" section. Include the final rule table, so the
   architect can review the policy without reading code.

## 6. When blocked
Stop and put the question under **Open questions** in your report, with what you have so far.

## 7. Report (the `finish` tool's `report` argument), in markdown
```
## Summary
## Plan vs. result
## Rule table   (input → decision → reason → test name)
## Deviations from the doc
## External APIs used
## Security-relevant decisions   (anything you were unsure about goes here, not silently into code)
## Tests added
## TODOs left for other packages
## Open questions
## Final check output   (last lines of ruff, mypy, pytest --cov)
```
