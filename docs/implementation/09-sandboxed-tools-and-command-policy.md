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
