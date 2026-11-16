# Agent tools: registry, file, command and git tools, audit

> Package **09b** · `gaia/tools/{registry,fs,shell,git_tools}.py`, `gaia/security/audit.py` ·
> current as of 2026-11-14

## What it's for
An agent changes a repository only by calling **tools**: read a file, edit a file, run a
command, look at git status. This package is the one implementation of those tools that **every**
runtime uses: the native loop (10b), the Claude Agent SDK (10c), and anything added later.

That is ADR-001's portability rule **P3** in code. The model and the agent loop are replaceable.
The tools, and with them policy, approvals, audit and events, stay Gaia's. Swapping Claude for
another model changes nothing in this directory.

## Files
| File | Role |
|---|---|
| `tools/registry.py` | `ToolRegistry` (the one dispatch path), `ToolContext`, `Tool`, `ToolResult`, `approve()`, `build_default_registry`, `ALL_TOOLS`/`JUNIOR_TOOLS` |
| `tools/fs.py` | `read_file`, `list_dir`, `write_file`, `edit_file`, `delete_file`, `search`, `find_rg` |
| `tools/shell.py` | `run_command`, the `CommandRunner` protocol, `SrtCommandRunner` (the only production runner), `command_env` |
| `tools/git_tools.py` | `git_status`, `git_diff`, `git_commit` (git is denied as a command) |
| `security/audit.py` | `AuditEntry`, `AuditSink`, `DbAuditSink`, `InMemoryAuditSink`, `summarize_args` |

## How it works

### One call, start to finish
A runtime gets a tool call from its model (`name`, `arguments`, an id) and passes it to
`registry.call(name, arguments, ctx, call_id=...)`:

```
registry.call
 ├─ emit agent.tool_call {call_id, tool, args: summarize_args(arguments)}
 ├─ arguments not an object?   → ok=False "arguments must be a JSON object"
 ├─ unknown name?              → ok=False "unknown tool: …"
 ├─ input_model.model_validate → ok=False "<loc>: <msg>" per error (extra fields refused)
 ├─ handler(model, ctx)
 │    ├─ PathDenied/UnsafePath → ok=False "path denied: …"
 │    └─ any other exception   → ok=False "tool failed: <Type>" (full trace only in the log)
 └─ finally, shielded and bounded at 10 s:
      write one audit row, cap content at 64 KB, emit agent.tool_result
```

The registry **never raises for anything the model did**. A model that calls a missing tool,
sends a wrong argument or trips a path guard gets a readable error back and can try again. Only
`asyncio.CancelledError` propagates, after the audit row and the result event are written.

### `ToolContext`: what a tool can reach
A runtime builds one per task. A handler can reach only what is in it:
- `task`: ids, role, `extra_allow` (the owner's test command) and the pinned `WorktreeLocation`;
- `git`, `policy`, `approvals`, `audit`, `events`;
- `commands`, the `CommandRunner`;
- `task_dir` (the command's `HOME` and `TMPDIR`), `actor` (`"agent:<name>"`) and `rg_path`.

`ctx.root` is the worktree. Every path a model sends goes through `confine(ctx.root, path)` before
it is used.

### File tools
- **Reads** cap at 200 KB. A **line range** scans up to 20 MB, so `start_line=19999` works on a
  big file. Binary files (a NUL byte in the first 8 KB) return `binary file, N bytes`.
- **Writes, edits and deletes** first run `classify_write`. CI workflows, lockfiles, `.env*` and
  agent configuration (`.claude/`, `.cline/`, `.mcp.json`, `.vscode/`…) return `ask`, and the tool
  waits **inside the turn** for a human via `approve()`. A rejection or timeout becomes `ok=False,
  "not approved: …"`; the agent is not stopped, it is told.
- Files are opened with `O_NOFOLLOW` after `confine`, so a symlink swapped in between the check
  and the open is refused, not followed.
- `edit_file` needs **exactly one** match of `old`. Zero or several matches are refused, with the
  count, so the model adds context instead of editing the wrong place.

### `search`: two modes
With ripgrep available (`Settings.rg_path`, or `rg` on `agent_path`), `search` runs a fixed rg
argv: `--no-config`, a 30 s timeout, 50 matches per file, no `.git`. The pattern is a **regex**.
Without ripgrep it falls back to Python, and the pattern is a **literal string**. The fallback
never runs `re` on a model's pattern, because a catastrophic-backtracking regex would pin a
server thread that cannot be killed. The tool description says which mode is active, because the
model must know.

### `run_command`
1. `ctx.policy.evaluate(argv, extra_allow=task.extra_allow)` (09a).
2. `deny` → `ok=False`, nothing runs. `ask` → `approve()` with the exact argv, cwd and reason. A
   rejection → `ok=False`.
3. `ctx.commands.run(...)`. In production that is `SrtCommandRunner`:
   - it builds a `SandboxSpec` (worktree read/write, task dir, toolchains readable, **no network**,
     Gaia's data dir denied);
   - writes the srt settings to a fresh 0700 directory under `{data_dir}/sandbox/`, **outside**
     anything the command can write, and deletes it afterwards;
   - launches `srt --settings … -- /usr/bin/env -u NO_PROXY -u no_proxy <argv>` with an environment
     built from nothing (`command_env`: `PATH` from `Settings.agent_path`, `HOME`/`TMPDIR` = task
     dir, `UV_NO_SYNC=1`, and `UV_PYTHON_INSTALL_DIR` = `Settings.sandbox_uv_python_dir` when that
     exists and a toolchain entry makes it readable).
4. The result is `exit=N` plus stdout and stderr sections, each capped at 64 KB. A failing test is
   `ok=False` but a normal result.

There is **no unsandboxed runner** in `gaia/`. Tests use `tests/tool_fakes.FakeCommandRunner`.

### Git tools
`git` is denied as a command (09a), because running it directly would skip `GitService`'s
hardened config and the F1 worktree pin. Agents get:
- `git_status` (porcelain, or "clean");
- `git_diff` (optionally one confined path, optionally `--cached`), via
  `GitService.diff_worktree`;
- `git_commit`, which stages everything and commits as `Gaia (<agent>) <gaia@localhost>`
  (`GitAuthor`, validated so a name cannot inject a header). It is **not** in `JUNIOR_TOOLS`: the
  graph commits accepted junior steps itself.

### Audit
Every call writes one `audit_log` row: `actor`, `action="tool.<name>"`, `target` (the path, or
`shlex.join(argv)`), and `details` (ids, summarised args, ok, decision, reason, duration).
`summarize_args` keeps the trail useful but bounded:
- a string over 256 characters becomes `{sha256, chars}`, so `write_file` content is never
  stored;
- short strings and argv items are redacted;
- nesting stops at depth 3.

`DbAuditSink` writes in its own transaction and **swallows** database errors (logged as
`audit.write_failed`). A DB hiccup must not crash an agent turn, and an agent must not be able to
stop its own audit trail by making a tool fail.

## Why it's built this way
- **One dispatch path.** If each runtime called handlers itself, each would need its own audit,
  events, truncation and error mapping, and they would drift. With one `call()`, the native loop
  and the SDK adapter produce identical audit rows for identical actions.
- **Shielded finally.** The SDK runs tool handlers inside an anyio task group. anyio
  cancellation is *level-triggered*: every `await` inside a cancelled scope is cancelled again,
  including the audit write in `finally`. `anyio.CancelScope(shield=True)` plus a 10 s
  `move_on_after` keeps the row and stays bounded. This is the same lesson as 07b's gateway
  metering. See [python-concepts](python-concepts.md) on cancellation.
- **Settings outside the sandbox.** The sandbox's rules are a file that `srt` reads at startup.
  If the sandboxed process can write that file, the boundary is only as strong as the agent's
  good behaviour.
- **Approvals inside the turn** (doc 10, requirement 8). Returning to the graph to ask would end
  the model session; a resumed node starts the whole step again.

## Changing it safely
- Add a tool: an input model (subclass `ToolInput`, so extra fields are refused), an async
  handler that returns `ToolResult`, `confine` every path, then register it in
  `build_default_registry` and decide whether it belongs in `JUNIOR_TOOLS`. Handlers may raise
  `PathDenied`; the registry maps it.
- **Never** import a vendor SDK here, and never mention a model (P1, P3).
- **Never** add a runner that skips `srt`. The Docker runner (18c) is another `CommandRunner`, also
  sandboxed.
- Tests: `tests/test_tool_registry.py` (dispatch, cancellation), `test_tools_{fs,search,shell,git}.py`,
  `test_audit.py`. Live: `GAIA_SRT_PATH=… uv run pytest tests/test_tools_live.py
  tests/test_sandbox_live.py`.

Related: [security core](security.md) (the policy and sandbox these tools call) ·
[runtimes](runtimes.md) (who builds the `ToolContext`) · [doc 09](../implementation/09-sandboxed-tools-and-command-policy.md) ·
[review 09b](../reviews/2026-11-14-review-09b.md).
