# 09 — Sandboxed Tools & Command Policy

**Split (2026-11-12 review):** **09a** covers `confine()`, the command policy and the sandbox
profile (tasks 1, 2, 5 and 6), as pure, exhaustively tested code for architect review. **09b**
covers the tools, registry and audit (tasks 3 and 4).

## Goal
The security core: path confinement, a command policy, and the tool set that native
runtimes use. Harness runtimes reuse the same policy through their permission hooks. The command
policy is a **tripwire, not a boundary**: allowlisted test runners execute code the agent wrote.
Containment comes from the OS sandbox (task 6).

## Out of scope
Container isolation (18).

## Files
- `gaia/security/paths.py`
- `gaia/security/commands.py`
- `gaia/security/audit.py`
- `gaia/tools/fs.py`, `gaia/tools/shell.py`, `gaia/tools/git_tools.py`, `gaia/tools/registry.py`
- `backend/gaia/security/command_policy.default.toml`

## Tasks
1. `confine(root: Path, user_path: str) -> Path`
   - Reject absolute paths, `~`, and NUL bytes. Join to root, then `resolve(strict=False)`. The result must
     satisfy `is_relative_to(root.resolve())`.
   - Deny the `.git` directory and anything under it.
   - Check symlinks: if the target exists and is a symlink that resolves outside root, deny.
2. Command policy (TOML), evaluated on the argv list:
   ```toml
   [[allow]]  # runs without approval
   cmd = "pytest"
   [[allow]]
   cmd = "git"; subcommands = ["status","diff","add","commit","log","show"]
   [[deny]]   # never runs
   cmd = "git"; subcommands = ["push","reset","clean","rebase","remote","config"]
   [[deny]]
   cmd = ["sudo","curl","wget","ssh","scp","nc","rm"]  # rm via the delete_file tool only
   # Anything else → requires_approval
   ```
   **Cut from v1 (D3):** repos do not supply policy. Per-repo allow rules (for example the
   repo's test command) are set by the owner and stored in the DB, never read from repo files.
   `evaluate(argv) -> Decision(allow|deny|ask, reason)`.
3. Tools (each is a Pydantic input model plus an async handler, registered with name, description and JSON schema):
   - `read_file(path, start_line?, end_line?)`, capped at 200 KB
   - `list_dir(path, depth≤3)`, ignoring `node_modules`, `.venv` and similar
   - `search(pattern, path?)`, implemented with `rg` through `run_proc` with a fixed argv
   - `write_file(path, content)`, `edit_file(path, old, new)` (exact single match, else error), `delete_file(path)`
   - `run_command(argv)` → policy → `run_proc` (timeout 300s, output cap 64 KB, scrubbed env, cwd = worktree)
   - `git_commit(message)`, which stages everything in the worktree and commits with author `Gaia <agent-name>`,
     through `GitService`'s hardened environment (repo hooks disabled, doc 05)
   - When policy says `ask`, the tool returns a `NeedsApproval` result. Doc 12 turns it into an interrupt.
4. Every tool call writes an audit row (tool, args with large content hashed, decision, duration).
5. Writes to `.github/workflows/**`, `**/*.lock`, `package-lock.json`, `.env*` and the `.gaia/` dir return `ask`.
6. **Sandbox profile (D1).** Build a `sandbox-runtime` (`srt`) config per task: writes to the
   worktree only; reads from the worktree and toolchains; no reads of `~/.ssh`, `~/.aws`, `~/.config` or
   `~/Library/Keychains`; network limited to the repo's allowlist plus the Gaia gateway. `run_command`
   and the harness runtimes (doc 10) launch through it.

## Tests (aim for full coverage of this module)
- Path escapes: `../x`, `/etc/passwd`, a symlink to outside, `a/../../x`, `.git/config`, `foo\0bar`.
- Policy: `git push` is denied, `pytest -k x` is allowed, `python script.py` asks, `bash -c "..."` asks.
- `edit_file` with zero or multiple matches errors.
- `run_command` env does not contain any secret value.
- With the sandbox on, a `conftest.py` the agent wrote that reads `~/.ssh` and calls `curl` fails
  under an allowlisted `pytest`. The sandbox blocks it; the policy alone would not.

## Acceptance criteria
- [ ] Reviewed by the architect, since this is security-critical code.

## Decisions and deviations (as built)

- **`UnsafePath` now lives in `gaia/security/paths.py`** and is re-exported from
  `gaia/services/git.py` (`from ... import UnsafePath as UnsafePath`). Existing imports keep
  working. `PathDenied(UnsafePath)` is raised for every path denied by `confine()`.
- **`Decision` lives in `gaia/security/decision.py`** so `paths.py` and `commands.py` share one
  frozen `action: Literal["allow","deny","ask"]` / `reason: str` type without one importing the
  other.
- **`git` is denied as a whole command**, not only for the doc's example deny list. Reason:
  `use the git tools (09b), which run through GitService's hardened environment`. This is a
  deliberate tightening from the doc's example: running `git` directly would skip
  `HARDENED_CONFIG` and the `WorktreeLocation` pin from 05b.
- **`~/.gaia/worktrees` itself is refused as an allowRead/allowWrite entry.** A worktree under
  `~/.gaia/worktrees/<...>` is allowed; the parent directory is not, because allowing it would
  expose every worktree at once.
- **Specific lockfile patterns are checked before `**/*.lock`** so the audit reason names
  `yarn.lock`, `uv.lock` or `poetry.lock` rather than the generic glob.
- **The live sandbox test is opt-in** (`GAIA_SRT_PATH`), because the test harness already runs
  inside `sandbox-exec` and macOS refuses to nest it. All non-live code reaches 100% coverage
  without it.

### Path rules (`gaia/security/paths.py`)

| Input | Decision | Reason |
|---|---|---|
| empty string, NUL byte | deny | `path is empty` / `path contains a NUL byte` |
| `/x`, `~`, `~root`, `C:`, `\\server` | deny | `absolute, home-relative or Windows paths are not allowed` |
| any component `.git` (case-insensitive) | deny | `the .git directory is reserved` |
| result not under `root.resolve()` | deny | `path resolves outside the root` |
| existing symlink component resolves outside root | deny | `path component is a symlink outside the root` |
| any other confined path | allow | returned resolved path |

`classify_write()` returns `ask` for `.github/workflows/**`, `**/*.lock`,
`**/package-lock.json`, `**/pnpm-lock.yaml`, `**/yarn.lock`, `**/uv.lock`,
`**/poetry.lock`, `.env` and `.env.*` at any depth, `.gaia/**`, `.claude/**`, `.cline/**`,
`.clinerules`, `.mcp.json`, `.vscode/**` and `.idea/**`; everything else is `allow`. Matching is
case-insensitive on the confined relative POSIX path.

### Command rules (`gaia/security/commands.py`)

Evaluation order (after the lead review): validate argv → unwrap wrappers (repeatedly, up to 4) →
deny → slashed-program ask → `extra_allow` → package-manager ask → shell/interpreter ask → TOML
allow rules → ask.

| Rule | Decision |
|---|---|
| argv empty / not a list of str / >256 items / item >4096 chars / NUL / newline in `argv[0]` | deny |
| `git` (any invocation, even `/usr/bin/git status`) | deny, use the git tools (09b) |
| `sudo su doas` / `curl wget ssh scp sftp rsync nc ncat netcat telnet ftp socat` / `rm rmdir dd mkfs shred diskutil` / `open osascript security launchctl defaults pbcopy pbpaste screencapture` / `crontab at` / `chown` / `chmod +s` or `u+s` / `kill pkill killall` | deny |
| `argv[0]` contains `/` | ask (never allow) |
| `uv`, `npm`, `npx`, `pip` outside the safe wrappers | ask |
| `bash sh zsh fish dash ksh env xargs exec eval nohup timeout nice time` | ask |
| `python script.py`, `python -c`, `node -e`, `perl`, `ruby`, `php`, `deno`, `bun` | ask |
| `uv run [--frozen|--offline|--no-sync|--quiet|-q] <cmd>` | evaluate `<cmd>` |
| `python -m <module>` / `python3 -m <module>` | evaluate `<module>` |
| `npx --no-install <cmd>` | evaluate `<cmd>` |
| TOML allow rule with no matching subcommand / with a `deny_flags` match | ask |
| TOML allow rule (see `command_policy.default.toml`) | allow |
| `extra_allow` starts-with match, element by element | allow only if the current decision is `ask` |
| anything else | ask |

The default TOML allow rules are: `pytest`, `ruff`, `mypy`, `black`, `isort`, `eslint`,
`prettier`, `tsc`, `vitest`, `jest`, `cargo` with subcommands
`test/check/build/fmt/clippy`, `go test`, `ls`, `cat`, `head`, `tail`, `wc`, `grep`, `rg`,
`find` with `-exec`, `-execdir`, `-delete`, `-ok` denied as flags, `diff`, `echo`, `pwd`,
`true`, `false`.

### Sandbox profile (`gaia/security/sandbox.py`)

- `denyRead`: the whole `home`, plus each sensitive path under it (`.ssh`, `.aws`, `.config`,
  `.gnupg`, `Library/Keychains`, `.gaia`, `.claude`, `.cline`, `.netrc`, `.git-credentials`,
  `.docker`, `.kube`, `.npmrc`, `.pypirc`, and the shell profiles).
- `allowRead`: worktree, `tmp_dir`, and existing `toolchain_read` paths only.
- `allowWrite`: worktree, `tmp_dir`, and `extra_write` only.
- `denyWrite`: `{worktree}/.git` (`literal: true`), `{worktree}/.github/workflows`,
  `{worktree}/.gaia`, `{worktree}/.claude`, `{worktree}/.cline`, `{worktree}/**/.env`,
  `{worktree}/**/.env.*`.
- Network: `allowedDomains` = repo allowlist plus `gateway`; entries may not have a scheme, path,
  whitespace, or be the bare `*`. `deniedDomains` is empty; local binding and unix sockets are off.
- Escape hatches `allowPty`, `allowAppleEvents`, `enableWeakerNestedSandbox` and
  `enableWeakerNetworkIsolation` are all explicitly `false`.
- `find_srt` uses `Settings.srt_path` if set, otherwise the pinned copy under
  `{data_dir}/tools/node_modules/.bin/srt`. It never searches `PATH`.

### Lead review changes (2026-11-14, [`review-09a`](../reviews/2026-11-14-review-09a.md))
- **S1:** `confine()` also refuses a path whose *resolved* location is inside `.git`. A symlink
  `alias -> .git` inside the worktree otherwise passed every check.
- **S2:** `extra_allow` (the owner-registered test command) is checked after the deny and slash
  rules but **before** the package-manager and shell tripwires, and its entries are unwrapped like
  the command. A bare `bash`/`python`/`npm` entry is ignored. The rule-table row above that says
  "allow only if the current decision is `ask`" is superseded by this.
- **S3:** `rg` denies `--pre`. A long deny flag also matches `--flag=value`. `find` also denies
  `-okdir`, `-fprint`, `-fprint0`, `-fprintf` and `-fls`.
- **S4:** wrappers unwrap repeatedly (`uv run python -m pytest` → `pytest`). Numeric setuid modes
  (`chmod 4755`, `02755`) are denied. `sudoedit` and `pkexec` are denied.
- **S5:** `SandboxSpec.data_dir` (Gaia's data dir, wherever it lives) is read-denied, and
  `data_dir/worktrees` itself cannot be allowed.
- **S6:** `.envrc`, `.pre-commit-config.yaml` and `.husky/**` writes are `ask`; `.envrc` is also
  write-denied in the sandbox.
- The live tests (`GAIA_SRT_PATH=… pytest tests/test_sandbox_live.py`) pass against `srt` 0.0.81:
  5 cases, including the `data_dir` read and a rewrite of a worktree's `.git` *file*.
- Acceptance ("reviewed by the architect") is pending the owner's review of the findings and
  decisions D-a and D-b in the review.

## Decisions and deviations (as built) — 09b

- **`approve(ctx, payload, reason, *, tool)` takes the tool name as a keyword.** The lead
  handoff's §2.2 call spelling omitted it, but §2.4 requires the tool name to come from a
  parameter rather than from the payload; the keyword form keeps it out of model-controlled data.
- **No `NeedsApproval` tool result and no graph interrupt.** Doc 09 task 3's last bullet predates
  doc 10 requirement 8 and is superseded: approvals are awaited inside the turn by the 10a broker.
- **`GitAuthor` is validated and `GitService.commit(author=)` passes the four `GIT_*` identity
  variables** through the pinned worktree environment; that is the only `git.py` change.
- ~~`git_diff` uses `GitService._command_context` and `_invoke` directly.~~ Superseded in lead
  review (R4): `GitService.diff_worktree(location, *, path, staged)` is now the public verb.
- **`Settings.agent_path` is the PATH for agents and sandboxed commands.** `build_agent_env`
  gains a `path=` keyword so existing callers keep the old hard-coded default; `command_env`
  passes `os.pathsep.join(settings.agent_path)`. The default includes `/opt/homebrew/bin` and
  `~/.local/bin` for Apple-silicon Macs and user uv installs.
- **`wrap_argv` now strips `NO_PROXY`/`no_proxy` inside the sandbox** with
  `-- /usr/bin/env -u NO_PROXY -u no_proxy <argv>`, so gateway traffic goes through srt's proxy
  instead of being refused by Seatbelt (ADR-001 consequence 1).
- **`/opt/homebrew` was not added to `DEFAULT_SANDBOX_TOOLCHAIN_READ`.** It is outside `home`, so
  it is readable already; a test asserts a spec containing it builds without weakening validation.
- **`SrtCommandRunner` is the only production runner and gives commands no network in 09b**
  (`allowed_domains=()`, `gateway=None`). Per-repo domains are a later setting.
- **Search has two modes.** With `rg_path` it runs the exact fixed rg argv from the handoff; with
  no `rg_path` it uses a fixed-string Python fallback (never `re` on a model pattern). The mode is
  written into the tool description so the model is told whether its pattern is a regex or literal.
- **`edit_file` reads the whole file, not the first 200 KB.** The 200 KB cap belongs to
  `read_file`; `edit_file` must be able to replace text anywhere in a file. Lead review (F2) caps
  it at `MAX_SCAN_BYTES` (20 MB).
- **Live tool tests** were added in `tests/test_tools_live.py`, skipped unless `GAIA_SRT_PATH` is
  set, mirroring `tests/test_sandbox_live.py`.

### Tool set (as built)
| Tool | Mutates | Junior | Notes |
|---|---|---|---|
| `read_file` | no | ✓ | 200 KB cap. A line range scans up to 20 MB, so it can reach past the cap |
| `list_dir` | no | ✓ | depth 1–3, symlinks shown but never followed, 2,000 entries |
| `search` | no | ✓ | ripgrep (regex) when `rg_path` resolves; otherwise a literal-substring Python fallback. The description says which |
| `write_file`, `edit_file`, `delete_file` | yes | ✓ | `classify_write` → `ask` for CI, lockfiles, `.env*`, agent config |
| `run_command` | yes | ✓ | `CommandPolicy.evaluate` → deny / ask (in-turn approval) / allow → `SrtCommandRunner` |
| `git_status`, `git_diff` | no | ✓ | through `GitService` and the pinned `WorktreeLocation` |
| `git_commit` | yes | — | lead only; the graph commits accepted junior steps (`git_commit_step`) |

### Lead review changes — 09b (2026-11-14, [`review-09b`](../reviews/2026-11-14-review-09b.md))
- **R1:** the registry's audit write and `agent.tool_result` event run **shielded** from
  cancellation, bounded at 10 s. Under the Agent SDK (10c) tool handlers run inside an anyio task
  group, where cancellation is level-triggered, and the audit row was lost. Reproduced by
  `test_audit_survives_a_level_triggered_cancel_scope`.
- **R2:** non-mapping arguments are refused as "arguments must be a JSON object", instead of
  raising out of the registry's `finally`.
- **S1:** `SrtCommandRunner` writes each command's srt settings to a fresh 0700 directory under
  `{data_dir}/sandbox/`, removed after the command. It used to write them inside the task dir,
  which the sandboxed command can write, so a process left behind by one command could rewrite
  the next command's sandbox rules.
- **F1:** a `read_file` line range now reaches past the first 200 KB, which the truncation note
  promised; output stays capped at 200 KB. **F2:** `edit_file` and ranged reads refuse files over
  20 MB. **F3:** file-tool errors no longer show the model absolute host paths.
- **R4:** `GitService.diff_worktree` replaces the tool's use of two private `GitService` methods.
- Verified live against srt 0.0.81: `tests/test_tools_live.py` and `tests/test_sandbox_live.py`
  (7 passed). Through `SrtCommandRunner`, `uv run --offline pytest --version` exits 0, which shows
  that `agent_path` finds the toolchain; an HTTPS request is refused by srt's proxy; and
  `cat ~/.zshrc` is denied.
- [ ] Architect review (security-critical): the tool layer above, plus 09a's open D-a/D-b.

### uv-managed Python across sandboxes (2026-11-15, bug fix)
- **Symptom (owner's live run on Gaia itself):** setup (`make setup-worktree`) succeeded, but every
  later `make test` in the same worktree, from the checks or the agent, failed with
  `failed to canonicalize path .venv/bin/python: Operation not permitted`.
- **Cause:** `command_env` sets `HOME` to the command's task directory. uv therefore looked for
  managed Pythons under `<data_dir>/runs/<run>/setup-tmp/.local/share/uv/python`, found none,
  downloaded one there, and the worktree's `.venv` linked to it. Every later command runs with a
  different task directory, and `data_dir` is denied in the sandbox, so the venv's interpreter
  could not be read.
- **Fix:** `Settings.sandbox_uv_python_dir` (default `~/.local/share/uv/python`, uv's own default
  for the real user). `command_env` sets `UV_PYTHON_INSTALL_DIR` to it when it exists and lies
  under a `sandbox_toolchain_read` entry (the default list already has `~/.local/share/uv`), so the
  sandbox gains no new read and no new write. Reproduced and verified against the real `srt`: with
  the fix the venv links to the owner's Python and a second sandbox runs it.
- **Limit:** if no suitable Python is installed there or on `PATH`, uv now fails in setup (it cannot
  write to the owner's directory) rather than in the first check. Install it once on the host with
  `uv python install 3.12`. Existing worktrees keep their broken venv until setup runs again (a new
  run) or the `.venv` is deleted.
