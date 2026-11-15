# Handoff: 09b — Sandboxed tools, registry, audit

Filled from `HANDOFF_TEMPLATE.md` for the lead's sandboxed DeepSeek harness (2026-11-14). Your file
tools are confined to the worktree. Your `run` tool is sandboxed: no network, no git writes, no
installs. The lead commits and merges; you never commit. Your report goes through `finish`.

---

You are a junior engineer on **Gaia**, a local-first control plane for AI coding agents. Your task
is to implement exactly one work package, **09b**: the tool layer every agent runtime uses. That
means file, search, command and git tools, one registry that validates, dispatches, audits and
emits events for every call, and the audit sink. 09a (path guard, command policy, sandbox profile)
is merged and is your foundation. The runtimes that call these tools are 10b and 10c; do not
build them.

**Why this package matters.** ADR-001 rule **P3** says Gaia owns the tool layer: the native
runtime (10b) and the Claude Agent SDK runtime (10c) both call **this** registry, so policy,
approvals, audit and events are identical whichever model runs. Nothing in this package may know
which runtime or vendor is calling.

## 1. Read first, in this order
1. `docs/implementation/09-sandboxed-tools-and-command-policy.md`: tasks 3 and 4, and the whole
   "Decisions and deviations (as built)" section, including "Lead review changes".
2. `docs/implementation/00-README.md`, "Rules for every work package".
3. `docs/decisions/ADR-001-agent-runtimes.md`: "Portability" (P1–P6) and "Consequences" 1–3.
4. `docs/implementation/10-agent-runtimes.md`: "Shared requirements" 2, 5, 8 and 11.
5. `docs/ARCHITECTURE.md` §6.
6. `docs/backend/README.md`, `security.md`, `runtimes.md`, `events.md`, `git-and-processes.md`,
   `python-concepts.md`.
7. `docs/reviews/2026-11-14-review-09a.md` and `docs/reviews/2026-11-14-review-10a.md`.
8. The code you build on:
   - `gaia/security/{paths,commands,sandbox,decision,redaction}.py`;
   - `gaia/services/{proc,git,events}.py`;
   - `gaia/runtimes/{base,approvals,support}.py`;
   - `gaia/db/{models,repo}.py` (`AuditLog`, `AuditLogRepo`), `gaia/domain/enums.py`;
   - `gaia/config/settings.py`;
   - `tests/test_sandbox.py`, `tests/test_sandbox_live.py`, `tests/test_runtime_approvals.py`,
     `tests/git_repo.py`, `tests/conftest.py`.

## 2. Lead notes for this package
These are design decisions already made. Follow them, and record any deviation in your report.

### 2.1 `gaia/tools/registry.py`: the one dispatch path
- `ToolResult` is a frozen dataclass:
  - `ok: bool` and `content: str`, the text the model sees;
  - `data: Mapping[str, Any] | None = None`, structured detail for events and tests;
  - `decision: Literal["allow","deny","ask_approved","ask_rejected"] = "allow"`.

  `content` is capped at `MAX_RESULT_CHARS = 64_000` by the registry, with a trailing
  `\n[truncated: N more characters]` note.
- `ToolContext` is a frozen dataclass. A runtime builds one per task:
  - `task: AgentTask` (from `gaia.runtimes.base`), which supplies the worktree, `extra_allow`, ids
    and role;
  - `git: GitService`, `policy: CommandPolicy` and `approvals: ApprovalBroker`;
  - `audit: AuditSink` (§2.5), `events: EventSink`, `commands: CommandRunner` (§2.3);
  - `task_dir: Path`: a per-task scratch dir the runtime creates. It is the sandbox `tmp_dir` and
    the command's `HOME`/`TMPDIR`;
  - `actor: str`, for example `"agent:junior-1"`; the audit row's actor;
  - `approval_timeout_s: float = 600`;
  - `rg_path: Path | None = None` (§2.2 `search`).

  A property `root` returns `task.worktree.work_tree`.
- `Tool` is a frozen dataclass:
  - `name: str` (`^[a-z][a-z0-9_]{1,40}$`), `description: str`;
  - `input_model: type[BaseModel]`; every input model sets `extra="forbid"`;
  - `handler: Callable[[BaseModel, ToolContext], Awaitable[ToolResult]]`;
  - `mutates: bool`.
- `ToolRegistry`:
  - `register(tool)` refuses a duplicate name (`ValueError`);
  - `get(name)` raises `UnknownTool(KeyError)`;
  - `names()`;
  - `subset(names) -> ToolRegistry` builds a new registry with just those tools, and raises
    `UnknownTool` for a missing name;
  - `specs() -> tuple[LLMTool, ...]` (from `gaia.llm.base`), built from
    `input_model.model_json_schema()`, in registration order. This is the only shape a runtime
    hands to a model. Keep it vendor-neutral JSON Schema.
  - `async call(name, arguments: Mapping[str, Any], ctx, *, call_id: str) -> ToolResult` is
    **the** dispatch path, and it never raises for anything the model did. In order:
    1. emit `EventType.AGENT_TOOL_CALL` with `{call_id, tool, args}`, where `args` is
       `summarize_args(arguments)` (§2.5);
    2. if the name is unknown, return `ok=False`, `"unknown tool: <name>"`;
    3. validate with `input_model.model_validate(arguments)`. On `ValidationError`, return
       `ok=False` and a one-line message per error (`loc: msg`), capped at 2,000 characters;
    4. run the handler. `PathDenied`/`UnsafePath` becomes `ok=False`,
       `"path denied: <reason>"`, decision `deny`. Any other `Exception` becomes `ok=False`,
       `"tool failed: <ExceptionType>"`, with the full exception logged through structlog
       (`tool.failed`), never sent to the model. `asyncio.CancelledError` is re-raised;
    5. **always** (in `finally`, including cancellation) write one audit row, then emit
       `EventType.AGENT_TOOL_RESULT` with `{call_id, tool, ok, decision, duration_ms,
       content}`, where `content` is the first 2,000 characters. The bus redacts and
       truncates as well (13).
- `build_default_registry(*, search_supports_regex: bool) -> ToolRegistry` registers every tool
  below. `JUNIOR_TOOLS` is a tuple of the names a junior gets: everything **except**
  `git_commit`, because the graph commits accepted steps itself (12, `git_commit_step`). Export
  `ALL_TOOLS` too.

### 2.2 `gaia/tools/fs.py`
Every path argument goes through `confine(ctx.root, path)` first. Writes and deletes also go
through `classify_write(ctx.root, resolved)`. On `ask`, call
`await approve(ctx, payload, reason)` (§2.4). If rejected, return `ok=False`,
`"not approved: <reason>"`, decision `ask_rejected`. If approved, continue with decision
`ask_approved`.

| Tool | Input model | Behaviour |
|---|---|---|
| `read_file` | `path: str`, `start_line: int \| None` (≥1), `end_line: int \| None` (≥ start) | Must be a regular file, otherwise an error. Read at most `READ_CAP_BYTES = 200_000` bytes. If the first 8,192 bytes contain a NUL, return `"binary file, <n> bytes"` with `ok=True`. Decode UTF-8 with `errors="replace"`. A line range is 1-based and inclusive. A file over the cap returns its first 200,000 bytes plus `[truncated: file is N bytes; use start_line/end_line]`. |
| `list_dir` | `path: str = "."`, `depth: int = 1` (1–3) | Must be a directory. Walk with `os.scandir` in sorted order. **Never follow symlinks**: show them as `name -> (symlink)`. Skip `IGNORED_DIRS = {".git", "node_modules", ".venv", "venv", "__pycache__", ".mypy_cache", ".pytest_cache", ".ruff_cache", ".tox", "dist", "build", ".next", "target", ".gradle", ".idea"}`. One entry per line, with paths relative to the root: `src/` for a dir, `src/a.py (1234 B)` for a file. Stop after `MAX_ENTRIES = 2000`, with a note. |
| `write_file` | `path: str`, `content: str` (≤ 1,000,000 chars) | Refuse an existing directory. Create missing parents, each confined, with `mkdir(parents=True)` after confining the final path. Write UTF-8. Return `"wrote <rel> (<n> bytes)"`. |
| `edit_file` | `path: str`, `old: str` (non-empty), `new: str` | Must be an existing file. Count occurrences of `old` exactly. 0 → `ok=False`, "old text not found"; more than 1 → `ok=False`, "old text matches N times; include more context". Otherwise replace the one occurrence and write it back. |
| `delete_file` | `path: str` | Regular files only; a directory is an error. `classify_write` applies. Return `"deleted <rel>"`. |
| `search` | `pattern: str` (1–500 chars), `path: str = "."`, `glob: str \| None` | See below. |

**`search`.** The doc says `rg` through `run_proc`, but ripgrep is **not** installed on every
machine. The lead's Mac has none, and the CI image is not guaranteed. So:
- **When `ctx.rg_path` is set:** call `run_proc` with exactly
  `[str(rg_path), "--no-config", "--line-number", "--no-heading", "--color=never",
  "--max-count=50", "--max-columns=300", "--max-filesize=1M", "--glob=!.git", *glob_args, "-e",
  pattern, "--", rel_path]`, where `glob_args` is `["--glob", glob]` if a glob is given. Run it
  with `cwd=ctx.root`, `env=base_environment()` minus `RIPGREP_CONFIG_PATH`, `timeout_s=30` and
  `max_output_bytes=64_000`. Exit 1 means "no matches". Exit 2 or a timeout gives `ok=False` with
  the stderr's first line. The `pattern` is a **regular expression** in this mode.
- **When `ctx.rg_path` is `None`:** a pure-Python fallback, run through `asyncio.to_thread`. It does
  **fixed-string** matching only, never `re` on a model-supplied pattern, because catastrophic
  backtracking could pin a server thread forever. It walks like `list_dir` with no depth limit,
  skips `IGNORED_DIRS`, symlinks, files over 1 MB and binary files, applies `glob` with
  `fnmatch` on the relative path, and caps output at 50 matches per file and 64,000 characters in
  total. The format is the same as rg's: `rel/path:LINE:text`, with each line cut at 300
  characters.
- `glob` must not be absolute and must not contain `..`; otherwise return `ok=False`.
- `build_default_registry(search_supports_regex=...)` puts the mode **into the tool's
  description**: "pattern is a regular expression (ripgrep syntax)" or "pattern is a literal
  string (exact substring match)". The model must be told which one it has.
- Add `find_rg(settings) -> Path | None` in `gaia/tools/fs.py`: use `Settings.rg_path` if set
  (it must be an executable file, otherwise raise `ValueError`), else
  `shutil.which("rg", path=os.pathsep.join(settings.agent_path))`.

### 2.3 `gaia/tools/shell.py`: `run_command`
- Input: `argv: list[str]` (1–256 items) and `timeout_s: int = 300` (1–1800).
- Flow:
  1. `decision = ctx.policy.evaluate(argv, extra_allow=ctx.task.extra_allow)`. Check the
     signature in `commands.py`.
  2. `deny`: `ok=False`, `"denied by policy: <reason>"`, decision `deny`. Nothing runs.
  3. `ask`: `approve(ctx, {"argv": argv, "cwd": str(ctx.root), "reason": decision.reason},
     decision.reason)`. Rejected: `ok=False`, `ask_rejected`. Approved: run it with decision
     `ask_approved`.
  4. Run it through `ctx.commands.run(argv, cwd=ctx.root, timeout_s=..., task_dir=ctx.task_dir)`
     and get back a `ProcResult`.
  5. `content` = `"exit=<code>"` (or `"timed out after <n>s"`), then `--- stdout ---` / `---
     stderr ---` sections, each with a `[output truncated]` note when `ProcResult` says so.
     `ok` is `returncode == 0 and not timed_out`. A failing test command is `ok=False` but
     still a normal result.
- `class CommandRunner(Protocol)`:
  `async def run(self, argv, *, cwd, timeout_s, task_dir) -> ProcResult`.
- `SrtCommandRunner(settings: Settings, *, srt_path: Path)` is the **only production
  runner**:
  1. Build `SandboxSpec(worktree=cwd, tmp_dir=task_dir, data_dir=settings.data_dir,
     toolchain_read=settings.sandbox_toolchain_read, allowed_domains=(), gateway=None)`.
     Commands get **no network** in 09b; per-repo domains are a later setting.
  2. `write_srt_settings(spec, task_dir / "srt")`.
  3. `run_proc(wrap_argv(srt_path, settings_path, argv), cwd=cwd, timeout_s=timeout_s,
     env=command_env(...), max_output_bytes=64_000)`.

  There is **no unsandboxed runner** in `gaia/`. Tests use a fake runner defined under `tests/`.
  Fail closed: building the context without a runner is a type error.
- `command_env(settings, task_dir) -> dict[str, str]` is `build_agent_env(home=task_dir,
  gateway_url=None, run_token=None, provider=None, path=...)` plus `UV_NO_SYNC=1`,
  `UV_CACHE_DIR=<task_dir>/uv-cache` and `PYTHONDONTWRITEBYTECODE=1`. Nothing else. It never
  reads `os.environ`.

**Two small changes to merged code that this needs (record both as deviations):**
1. **Agent `PATH`.** `gaia/runtimes/support.py`'s `AGENT_PATH` is
   `/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin`. On Apple-silicon Macs `uv`, `node` and `git`
   live in `/opt/homebrew/bin`, and in `~/.local/bin` for a uv install, so `uv run pytest` would not
   be found.
   - Add `Settings.agent_path: tuple[str, ...]`, defaulting to `DEFAULT_AGENT_PATH =
     ("/opt/homebrew/bin", "/usr/local/bin", "~/.local/bin", "/usr/bin", "/bin", "/usr/sbin",
     "/sbin")`. Expand `~` with a validator. Entries must be absolute after expansion, otherwise
     a `ValueError`.
   - Give `build_agent_env` a keyword `path: str = AGENT_PATH`. Existing callers keep the old
     default; `command_env` passes `os.pathsep.join(settings.agent_path)`.
   - Add `Path("/opt/homebrew")` to `DEFAULT_SANDBOX_TOOLCHAIN_READ` only if
     `build_srt_settings` would otherwise refuse it. It is outside `home`, so it is readable
     already; just add a test that a spec with `/opt/homebrew` in `toolchain_read` builds.
2. **`wrap_argv` and `NO_PROXY`** (ADR-001 consequence 1). srt sets `NO_PROXY` for loopback, and
   clients then bypass its proxy and get `EPERM`. Change `wrap_argv` to return `[srt,
   "--settings", settings, "--", "/usr/bin/env", "-u", "NO_PROXY", "-u", "no_proxy", *argv]`.
   Update `tests/test_sandbox.py`'s expectation, and keep `tests/test_sandbox_live.py` working.
   Its cases call `wrap_argv`; check that they still assert the same outcomes.

### 2.4 Approvals: `approve(ctx, payload, reason) -> ApprovalDecision`
A helper in `registry.py`. It calls `await ctx.approvals.request(ctx.task,
kind=ApprovalKind.COMMAND, payload={**payload, "tool": <tool name>, "reason": reason},
timeout_s=ctx.approval_timeout_s)`. The tool name comes from a parameter, not from the payload.
That is all. The broker (10a) already upserts the row, publishes, waits in the turn, and treats a
timeout as deny. **No `NeedsApproval` result type and no graph interrupt.** Doc 09 task 3's last
bullet predates doc 10's requirement 8 and is superseded; record that as a deviation. File-write
approvals use the same `command` kind, with `{"tool": "write_file", "path": rel, ...}`. The
`approval.kind` enum is not extended.

### 2.5 `gaia/security/audit.py`
- `AuditEntry` is a frozen dataclass: `workspace_id`, `actor`, `action` (`"tool.<name>"`),
  `target: str | None` (the relative path, or `shlex.join(argv)`, capped at 500), and `details:
  Mapping[str, Any]`. The details hold `task_id`, `run_id`, `call_id`, `args` (summarised),
  `ok`, `decision`, `reason` and `duration_ms`.
- `class AuditSink(Protocol)`: `async def record(self, entry: AuditEntry) -> None`.
- `DbAuditSink(sessions)` appends through `AuditLogRepo` in **its own short transaction**, like
  `EventBus` does. An audit write must not depend on the caller's session, and a failing audit
  write is logged (`audit.write_failed`), never raised into the tool. An agent must not be able
  to break its own audit trail by making a tool fail; equally, a DB hiccup must not crash the
  agent mid-turn. Look at how `gaia/services/events.py` opens its session.
- `InMemoryAuditSink` keeps `entries: list[AuditEntry]`; tests use it.
- `summarize_args(args: Mapping[str, Any]) -> dict[str, Any]` returns a new dict:
  - a `str` value longer than 256 characters becomes `{"sha256": <hex>, "chars": <len>}`;
  - other strings are passed through `redact`;
  - lists and tuples of strings (argv) are redacted element by element, and over 256 items are
    replaced by a hash;
  - nested mappings recurse to depth 3, after which the value becomes the string `"…"`;
  - numbers, bools and `None` pass through.

  `write_file`'s `content` therefore never reaches the DB or an event.

### 2.6 `gaia/tools/git_tools.py`
Git is denied in `run_command` (09a). Agents get these instead, all through `ctx.git` and
`ctx.task.worktree` (the pinned `WorktreeLocation`, finding F1):

| Tool | Input | Behaviour |
|---|---|---|
| `git_status` | none (an empty model) | `git.status_porcelain(location)`; "clean" if empty |
| `git_diff` | `path: str \| None` (confined if given), `staged: bool = False` | Use `git.diff(...)`; read its signature. Cap the output at 64,000 characters with a note |
| `git_commit` | `message: str` (1–2,000 chars, no NUL; the first line ≤ 200) | `git.commit(location, message, add_all=True, author=GitAuthor(name=f"Gaia ({agent})", email="gaia@localhost"))`, where `agent` is `ctx.actor` with the `"agent:"` prefix stripped. "Nothing to commit" (a `GitError` whose output says so) becomes `ok=False`, "nothing to commit". Return the short SHA. `mutates=True` |

Extend `GitService.commit` with a keyword `author: GitAuthor | None = None`.
- `GitAuthor` is a frozen dataclass in `git.py` with `name` and `email`. Validate both: no
  newline, `<`, `>` or NUL, and at most 100 characters.
- When it is given, pass `GIT_AUTHOR_NAME`, `GIT_AUTHOR_EMAIL`, `GIT_COMMITTER_NAME` and
  `GIT_COMMITTER_EMAIL` through `_command_context`'s env dict, merged into the extra env.

This is the only change in `git.py`.

### 2.7 Testability seams (use them)
- Build worktrees with `tests/git_repo.py`'s helpers and a real `GitService`, as
  `tests/test_runtime_contract.py` does. Use a real `WorktreeLocation`, never a mocked one.
- `FakeCommandRunner` (in `tests/`) records calls and returns a canned `ProcResult`.
- `InMemoryApprovalBroker` from 10a: answer it from a concurrent task, the way
  `tests/test_runtime_approvals.py` does.
- A recording `EventSink` fake (`emit` appends to a list).
- For the rg path, write a tiny **fake `rg`** script into `tmp_path` (`#!/usr/bin/env python3`,
  `chmod 0o755`) that records its argv to a file and prints canned output. Then assert the exact
  argv, exit 1 meaning no matches, and exit 2 meaning an error. Do not depend on a real `rg`
  being installed: coverage must not depend on the machine (lesson from 10a).
- **Live sandbox test (you cannot run it):** add `tests/test_tools_live.py`, skipped unless
  `GAIA_SRT_PATH` is set, mirroring `tests/test_sandbox_live.py`. It runs `SrtCommandRunner`
  with an allowlisted command (`["python3", "-c", ...]` is `ask`, so call the runner directly,
  not through the policy). The command tries to (a) read `~/.ssh/known_hosts` or another denied
  path and (b) open a TCP connection to `1.1.1.1:443`, and the test asserts both fail. A second
  case runs `["pytest", "--version"]`, or whatever the live sandbox test already uses, and
  asserts exit 0. The lead runs this file after review.

## 3. Files
**You may create:**
- `gaia/tools/registry.py`, `gaia/tools/fs.py`, `gaia/tools/shell.py`, `gaia/tools/git_tools.py`
  (`gaia/tools/__init__.py` exists; you may add a docstring);
- `gaia/security/audit.py`;
- `tests/test_tool_registry.py`, `tests/test_tools_fs.py`, `tests/test_tools_search.py`,
  `tests/test_tools_shell.py`, `tests/test_tools_git.py`, `tests/test_audit.py`,
  `tests/test_tools_live.py`, and `tests/tool_fakes.py` for shared fakes.

**You may edit:**
- `gaia/security/sandbox.py` (`wrap_argv` only) and `tests/test_sandbox.py`;
- `gaia/runtimes/support.py` (`build_agent_env`'s `path` keyword) and its tests;
- `gaia/services/git.py` (`GitAuthor` and `commit(author=)` only) and `tests/test_git*.py`;
- `gaia/config/settings.py` (`agent_path`, `rg_path`) and `tests/test_settings.py`;
- `docs/implementation/09-sandboxed-tools-and-command-policy.md`: add a "Decisions and deviations
  (as built) — 09b" section at the end;
- `.env.example`, if it lists settings (check).

**You must not touch:**
- `pyproject.toml`, `uv.lock` and every `[tool.*]` section;
- `.github/`, Alembic migrations, `gaia/api/`, `gaia/orchestrator/`;
- the other docs (the lead writes the guides and review);
- `backend/spikes/`.

No new dependencies. Everything you need is installed.

## 4. Non-negotiable rules
- Never print, log or commit a secret. Tool results that echo command output are fine; the event
  bus redacts. Never put a secret in a test fixture that looks real: use `sk-ant-planted-...`
  style values, as existing tests do.
- No `shell=True`, no `subprocess`. Only `gaia/services/proc.py` spawns processes (ruff TID251
  enforces it).
- SQL only through `gaia/db/` repositories.
- **Fail closed.** An unknown tool, invalid input, a path that cannot be confined, an approval
  timeout, a missing sandbox: each one refuses the action.
- Check library APIs against the installed source in
  `backend/.venv/lib/python3.12/site-packages/` (pydantic 2, structlog), not from memory.
- **No vendor names in this package.** The tools must not import `anthropic`, `openai` or
  `claude_agent_sdk`, and must not mention a model (ADR-001 P1, P3).
- Type everything (mypy strict). No new `# type: ignore` or `# noqa` without a reason comment.

## 5. How to work
1. Read §1, then write a short plan as your first message: the modules, their order, and the
   test files.
2. Build in this order: `audit.py` → `registry.py` (with a dummy tool in tests) → `fs.py` →
   search → `shell.py` and the `wrap_argv`/`agent_path` changes → `git_tools.py` and
   `GitService.commit(author=)` → `build_default_registry`.
3. **Required tests** (names may vary; each behaviour must be covered):
   - registry: an unknown tool; invalid args, including an extra field; a handler exception
     becomes a generic message and is logged, not leaked; `PathDenied` becomes `path denied`;
     an audit row and both events for every call, **including** failed and cancelled ones; content
     truncation; `specs()` JSON schemas; `subset`; duplicate registration;
   - path escapes through every fs tool: `../x`, `/etc/passwd`, a symlink to outside, `.git/config`
     and a NUL byte, each refused with no file touched;
   - `edit_file` with zero and with two matches;
   - `write_file` to `.github/workflows/ci.yml` asks: approved, it writes; rejected, the file is
     untouched; a timeout counts as rejected;
   - `read_file` on a binary file, a line range, and a file over the cap;
   - `list_dir` does not follow a symlinked dir, skips `node_modules`, and stops at depth;
   - search: the fake-rg argv is exact; exit 1 means no matches; exit 2 is an error; the Python
     fallback does fixed strings (a pattern `a.c` does **not** match `abc`); the glob filter; a
     glob containing `..` is refused; the description text differs by mode;
   - `run_command`: `git push` is denied with no runner call; `pytest -k x` is allowed and runs;
     `python script.py` asks (approved runs, rejected does not); `extra_allow` lets the owner's
     command through; a timeout's content; output truncation notes;
   - `command_env` holds no variable planted in `os.environ` (for example
     `ANTHROPIC_API_KEY=sk-ant-planted`), and `PATH` comes from `settings.agent_path`;
   - `SrtCommandRunner` builds the exact wrapped argv and writes a 0600 settings file; mock
     `run_proc` at the module boundary;
   - git tools: status clean or dirty, a confined diff path, a commit with the author check
     (`git log -1 --format=%an <%ae>`), nothing to commit, an invalid author refused;
   - `summarize_args`: long content hashed, argv redacted, depth cap;
   - `DbAuditSink`: a row is written in its own transaction, and a failure is logged and swallowed.
4. Checks, all of which must pass (run them from `backend/`):
   - `uv run ruff check .`
   - `uv run ruff format --check .`
   - `uv run mypy`
   - `uv run pytest -q`, at 100% coverage (`fail_under` is enforced).

## 6. When blocked
Do not guess at architecture. Write the question under "Open questions" in your report, choose the
most restrictive reasonable behaviour, and keep going.

## 7. Report (pass to `finish`, in Markdown, with these headings)
- **Summary**
- **Files changed**
- **Deviations** from this handoff or doc 09, each with a reason
- **External APIs used**: library calls, with the installed file you checked
- **Security-relevant changes**: every place a decision is made to allow, deny or ask
- **Tests added**: names and what each one proves
- **Open questions**
- **Final check output**: the last lines of each of the four checks
