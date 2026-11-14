# Security core: paths, commands, sandbox

> Package **09a** · `gaia/security/{paths,commands,sandbox,decision}.py`,
> `gaia/security/command_policy.default.toml` · current as of 2026-11-14

## What it's for
Agents edit files and run commands in a repository Gaia does not trust. Three pure modules decide
what an agent may touch:

| Question | Module | Answer |
|---|---|---|
| "Is this path inside the worktree, and not `.git`?" | `paths.confine()` | the resolved `Path`, or `PathDenied` |
| "May the agent write this file without asking?" | `paths.classify_write()` | `Decision(allow \| ask)` |
| "May the agent run this command?" | `commands.CommandPolicy.evaluate()` | `Decision(allow \| deny \| ask)` |
| "What can a command it runs actually reach?" | `sandbox.build_srt_settings()` | an `srt` settings dict (the OS boundary) |

None of these modules perform I/O beyond reading the filesystem to resolve paths. The tools that
use them (09b) and the runtimes (10) call them before every action and write an audit row with the
`Decision.reason`.

## The one idea to hold on to: tripwire versus boundary
The **command policy is a tripwire**. `pytest` is allowed, and `pytest` imports `conftest.py`,
which the agent can write, so "pytest is allowed" means "any Python is allowed". The policy's job
is to catch the obvious things early, with a readable reason, and to route unusual commands to a
human (`ask`).

The **sandbox is the boundary**. Every command runs under Anthropic's `sandbox-runtime` (`srt`),
which uses macOS Seatbelt (`sandbox-exec`) to enforce file and network rules in the kernel,
whatever the command does. The live test in `tests/test_sandbox_live.py` proves exactly this: a
`conftest.py` that reads an SSH key and opens a socket fails under an *allowed* `pytest`.

## Files
| File | Role |
|---|---|
| `decision.py` | `Decision(action, reason)`, a frozen dataclass shared by both policies |
| `paths.py` | `confine`, `classify_write`, a small glob matcher, `UnsafePath`/`PathDenied` |
| `commands.py` | `CommandPolicy`: argv validation, wrapper unwrapping, deny list, tripwires, allow rules |
| `command_policy.default.toml` | the **allow** rules only. Deny and ask rules are code, so no config edit can weaken them |
| `sandbox.py` | `SandboxSpec` → `build_srt_settings` → `write_srt_settings` (mode 0600) → `wrap_argv`; `find_srt` |

## How it works

### `confine(root, user_path)`
1. Refuse the obvious, before touching the filesystem:
   - an empty path or a NUL byte;
   - an absolute path, `~`, `C:` or `\\server`;
   - any `.git` component, case-insensitive (APFS is case-insensitive, so `.GIT` *is* `.git`).
2. Walk each component from the root. If an existing component is a symlink that resolves outside
   the root, refuse it. This catches `link/file`, where only the parent is a symlink.
3. Resolve the whole path, and require that it is still under the resolved root.
4. Check the **resolved** path for `.git` again. A symlink `alias -> .git` makes the input look
   harmless while it lands in `.git`.

Paths are resolved *before* use, but a file can still change between the check and the write.
That race is why 09b's tools must also open files without following symlinks, and why the sandbox
denies `.git` writes regardless.

### `CommandPolicy.evaluate(argv, extra_allow=())`
The order matters. Each step can end the evaluation:

```
validate argv (non-empty list of str, ≤256 items, ≤4096 chars each, no NUL)    → deny
unwrap: uv run [--frozen|--offline|--no-sync|-q] X · python -m X · npx --no-install X
        (repeatedly, up to 4 levels: `uv run python -m pytest` → `pytest`)     → ask if odd
deny list: git, sudo/su/pkexec, curl/ssh/nc…, rm/dd…, open/osascript/security…,
           crontab/at, chown, chmod setuid (+s, u+s, 4755), kill…               → deny
argv[0] contains "/" (./pytest could be agent-written)                         → ask
extra_allow (owner-registered repo test command, also unwrapped)               → allow
package managers (uv, npm, npx, pip)                                           → ask
shells, interpreters, process wrappers (bash, python script.py, node -e, env…) → ask
TOML allow rules (pytest, ruff, mypy, cargo test…, rg minus --pre, find minus -exec…) → allow
anything else                                                                  → ask
```

Why is `git` denied outright? Running git directly would skip `GitService`'s hardened
configuration and the worktree pin from 05b (finding F1). Agents use the git tools (09b) instead.

Why can `extra_allow` beat the shell tripwire? It is the **owner's** registered test command
(D3: never read from the repo). Registering `bash scripts/test.sh` is a deliberate decision, and
it adds no risk beyond `pytest` running an agent-written `conftest.py`. A bare `bash`, `python` or
`npm` entry is ignored, so a registration can never mean "allow everything".

### `build_srt_settings(spec)`
- **Reads:** deny all of `home`, explicitly deny the sensitive paths and Gaia's `data_dir` wherever
  it lives, then re-allow the worktree, the task's tmp dir and existing toolchain caches. In srt,
  `allowRead` beats `denyRead`, which is why validation refuses any allow entry that is `home`, `/`,
  an ancestor of a sensitive path, or `data_dir/worktrees` itself.
- **Writes:** only the worktree, the tmp dir and `extra_write`. In srt, `denyWrite` beats
  `allowWrite`, so `.git` (as a literal path), `.github/workflows`, `.gaia`, `.claude`, `.cline`,
  `.env*` and `.envrc` stay read-only even inside the worktree.
- **Network:** nothing except the repo's allowlist and the gateway. The gateway is given as an IP
  literal (`127.0.0.1:8765`), because srt refuses hostnames that resolve to loopback.
- **Escape hatches** (`allowAppleEvents`, `allowPty`, the weaker nested and network modes) are always
  `false`. `allowAppleEvents: true` would let a command `open` an application outside the sandbox.
- `srt` is pinned to **0.0.81**. `make sandbox-install` puts it under `$GAIA_DATA_DIR/tools`, and
  `find_srt` never falls back to `PATH`, which could pick up an unpinned copy.

## Changing it safely
- **Adding an allow rule** is a TOML edit plus a row in `tests/test_security_commands.py`. Adding a
  deny or ask rule is a code change, on purpose.
- **Any change to `sandbox.py` needs the live tests**:
  `GAIA_SRT_PATH=<path to srt> uv run pytest tests/test_sandbox_live.py`. They are skipped in CI,
  because CI has no `srt`, and the lead's harness cannot nest `sandbox-exec`.
- **Never** add a `~` path to the generated settings (srt expands `~` with its own `HOME`). Never
  set an escape hatch to `true`, and never allow `home` or `data_dir`.
- A new sensitive location (a new credential store) goes in `_SENSITIVE_RELATIVE`, with a test.

See also: [Python concepts → frozen dataclasses and Pydantic models](python-concepts.md), and the
review that hardened this code, [`docs/reviews/2026-11-14-review-09a.md`](../reviews/2026-11-14-review-09a.md).
