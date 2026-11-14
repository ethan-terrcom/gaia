# Handoff: 10a — Runtime base types, contract suite, fake runtimes

Filled from `HANDOFF_TEMPLATE.md` for the lead's sandboxed DeepSeek harness (2026-11-14). File tools
are confined to the worktree. The `run` tool is sandboxed: no network, no git writes, no installs.
The lead does the commit and merge, and the report goes through `finish`.

---

You are a junior engineer on **Gaia**, a local-first control plane for AI coding agents. Your task is
to implement exactly one work package, **10a**: the `AgentRuntime` seam, its shared machinery, a
contract test suite, and two fake runtimes. Real runtimes (native DeepSeek, Claude Agent SDK) are
**10b+**, after ADR-001; do not build them. The lead engineer reviews your work, then commits and
merges it. You never commit.

## 1. Read first, in this order
1. `docs/implementation/10-agent-runtimes.md`: the split note, "Shared requirements" 1–11, and
   "Tests". This is your contract, minus the per-runtime sections.
2. `docs/implementation/00-README.md`, "Rules for every work package".
3. `docs/ARCHITECTURE.md` §3, §4 and §6.
4. `docs/backend/README.md`, then `docs/backend/git-and-processes.md`, `events.md`, `gateway.md`,
   `security.md` and `python-concepts.md`.
5. The code you build on:
   - `gaia/services/{proc,git,events}.py`, `gaia/llm/run_tokens.py`;
   - `gaia/security/{commands,sandbox}.py`;
   - `gaia/db/{models,repo}.py`, `gaia/domain/enums.py`;
   - `gaia/cli.py`, `gaia/config/settings.py`;
   - `tests/test_proc.py`, `tests/git_repo.py`, `tests/conftest.py`.

## 2. Lead notes for this package
These are design decisions already made. Follow them, and record any deviation.

**`gaia/runtimes/base.py`**
- `AgentTask` is a frozen Pydantic model:
  - `task_id: str` (a uuid7), `run_id`, `run_step_id: str | None`, `workspace_id`,
    `agent_id: str | None`;
  - `role: AgentRole` and `instructions: str`;
  - `worktree: WorktreeLocation` (from `gaia/services/git.py`; set
    `arbitrary_types_allowed`), plus `base_sha: str`;
  - `timeout_s: float = 1800` and `max_turns: int = 40`;
  - `extra_allow: tuple[tuple[str, ...], ...] = ()`, the owner's repo test command (09a D3);
  - `output_schema: dict[str, Any] | None = None`.
- `AgentResult` is a frozen model:
  - `status: Literal["completed","failed","timeout","cancelled","budget_exceeded"]`;
  - `summary: str` and `structured_output: dict[str, Any] | None`;
  - `files_changed: tuple[str, ...]`;
  - `usage: RuntimeUsage` (input, output and cache tokens plus `cost_usd: Decimal`, summed over the
    task);
  - `error: str | None` (a short, redacted message), `transcript_ref: str | None`.
- `class AgentRuntime(Protocol)` has `name: str`,
  `async def run_task(self, task: AgentTask, events: EventSink) -> AgentResult` and
  `async def cancel(self, task_id: str) -> None`. Import `EventSink` from `gaia.services.events`.
  Do not define a second one.
- **The registry** is `RuntimeRegistry`: `register(runtime)`, `get(name)` (raises `UnknownRuntime`)
  and `names()`. It is a plain object built in the lifespan, not a module global.

**`gaia/runtimes/approvals.py`** (requirement 8: command approvals stay inside the turn)
- `ApprovalDecision` is a frozen dataclass of `approved: bool` and `reason: str`.
- `class ApprovalBroker(Protocol)`: `async def request(self, task: AgentTask, *, kind: ApprovalKind,
  payload: Mapping[str, Any], timeout_s: float) -> ApprovalDecision`. A timeout means
  **deny** (`approved=False, reason="timed out"`).
- `InMemoryApprovalBroker` is for tests and fakes. It exposes `pending()` and
  `decide(approval_id, approved, reason)` so a test can answer from another task.
- `DbApprovalBroker(sessions, bus)` is the real one:
  1. **upsert** an `approval` row (`kind`, `payload_json`, status `pending`) with
     `idempotency_key=f"cmd:{task.task_id}:{sha256(canonical json payload)[:16]}"`, using the
     unique constraint from 05b;
  2. publish `EventType.APPROVAL_REQUESTED` with the approval id;
  3. await an `asyncio.Event` registered in an in-process `dict[approval_id, waiter]`;
  4. a `decide(workspace_id, approval_id, approved, comment, user_id)` method sets the row
     (`approved`/`rejected`, `decided_by`, `decided_at`), publishes `APPROVAL_DECIDED` and wakes
     the waiter. Doc 14's HTTP route will call it.

  On timeout, mark the row `expired`.

**`gaia/runtimes/support.py`** (shared requirements as small, separately tested functions)
- `build_agent_env(*, home: Path, gateway_url: str | None, run_token: SecretStr | None,
  provider: Literal["anthropic","deepseek"] | None) -> dict[str, str]` (requirement 2).
  - Build the dict **from scratch**: `PATH` (a fixed system path, never the parent's), `HOME`
    (the per-task temp dir), `LANG=C.UTF-8`, `TMPDIR=home`.
  - For a provider, add `ANTHROPIC_BASE_URL` + `ANTHROPIC_API_KEY=<run token>`, or
    `OPENAI_BASE_URL` + `OPENAI_API_KEY=<run token>`.
  - It never reads `os.environ`. Test that no value of a variable you plant in `os.environ` (for
    example `ANTHROPIC_API_KEY=sk-ant-planted…`) appears in the result.
- `async def files_changed(git: GitService, location: WorktreeLocation, base_sha: str) ->
  tuple[str, ...]` (requirement 6) covers committed, staged, unstaged **and untracked** changes
  since `base_sha`.
  - Add one method to `GitService`:
    `changed_paths(location, base) -> tuple[str, ...]`, which runs
    `git diff --name-only --no-ext-diff --no-textconv <base>` and
    `git ls-files --others --exclude-standard`, then sorts and de-duplicates.
  - It is the only change you make in `git.py`. Use `check_ref_argument` and `_command_context`
    as the other verbs do.
- `async def run_with_limits(runtime, task, events, *, now=time.monotonic) -> AgentResult`
  enforces requirements 3 and 7 for **every** runtime, so no runtime re-implements them.
  - It awaits `runtime.run_task` under `asyncio.timeout(task.timeout_s)`. On expiry it calls
    `runtime.cancel(task.task_id)` and returns `status="timeout"`.
  - It emits `agent.message` and `agent.usage` events around the call, catches unexpected
    exceptions into `status="failed"` with a redacted error, and computes `files_changed` itself
    (requirement 6: never trust the agent).
  - A `CancelledError` from outside (the run was cancelled) must still call `runtime.cancel` and
    then re-raise.

**`gaia/services/proc.py`**: add `stream_proc` (doc 10's Cline note; harness runtimes need it).
- `async def stream_proc(argv, cwd, *, env, on_line: Callable[[str, str], Awaitable[None]],
  timeout_s, max_line_bytes=65536) -> ProcResult` behaves like `run_proc`:
  - no shell, a new process group, the same `_check_argv`, and `_kill_process_group` plus
    `_reap_after_kill` on timeout and cancellation;
  - it calls `on_line("stdout"|"stderr", line)` for each line as it arrives;
  - over-long lines are split, and the result holds only the last 64 KB of each stream.
- `pgid_of(result)` is not needed. Instead, give `stream_proc` an `on_start: Callable[[int],
  None] | None` that receives the pid (= pgid) right after spawn, for orphan tracking.

**`gaia/runtimes/orphans.py`** (requirement 9)
- `OrphanRegistry(path)` is a JSON file at `{data_dir}/runtime-pids.json`, mode 0600. Each entry is
  `{pid, task_id, started_at (epoch), argv0}`.
- `add`, `remove` and `async reap_stale(*, is_ours)`:
  - for each entry, if the process exists and `is_ours(entry)` holds, kill the process group;
  - then drop the entry.
- `is_ours` compares the process's start time and command, read through
  `run_proc(["ps", "-o", "lstart=,comm=", "-p", str(pid)])`, against the entry. **Never kill a pid
  you cannot verify**: pids get reused.

**Fake runtimes** (in `gaia/runtimes/fake.py`; production code, so `gaia dev run-task` can use them)
- `FakeRuntime(script: Sequence[FakeAction])`, where an action is one of:
  - `Emit(type, payload)`, `WriteFile(path, content)` (through `security.paths.confine`);
  - `RequestApproval(argv)` (through the `ApprovalBroker` given at construction; a denial is
    recorded in the summary, and the script continues);
  - `Sleep(seconds)`, `Fail(message)` and `Finish(summary, structured_output)`.
- `FakeProcessRuntime(script_path, broker)` runs `python <gaia/runtimes/fake_agent.py>` through
  `stream_proc`, with `build_agent_env(...)`, in the worktree. `fake_agent.py` is a tiny
  stdlib-only program that reads a JSON script from argv, prints JSON lines
  (`{"type":"message",...}`, `{"type":"write",...}`, `{"type":"env"}` dumps its environment
  **names and values** to stdout for the env test, `{"type":"sleep"}`), and writes files. The
  runtime maps lines to events, and registers and unregisters the pid in the `OrphanRegistry`.
  `cancel()` kills the group.

**Contract suite** (`tests/runtime_contract.py` plus `tests/test_runtime_contract.py`)
- Parametrise over both fakes with a fixture factory. Every test goes through
  `run_with_limits`. The tests:
  - timeout gives `status="timeout"`, and the process is gone;
  - cancel mid-run gives `status="cancelled"` within 5 s;
  - the env scrub: a planted secret is absent, and no real provider key is present
    (`FakeProcessRuntime` only);
  - `files_changed` matches git, including an untracked file, and ignores the agent's claim;
  - events are emitted in order;
  - an in-turn approval, approved and denied, with the broker answered from a concurrent task;
  - orphan cleanup: a registry entry for a live fake-agent process is reaped, and an entry whose
    pid is not ours (for example the test process itself, with a mismatched `argv0`) is **not**
    killed.

**CLI:** `gaia dev run-task --runtime fake --worktree <path> "<instructions>"` builds a scratch run
in the DB if needed, runs `FakeRuntime` with a short script that writes `hello.py` and emits
events, and prints the `AgentResult` JSON. Real runtimes come in 10b.

**Settings:** add `runtime_default_timeout_s: float = 1800` only if you need it; prefer task fields.

## 3. What you may and must not touch
**You may create or edit only:**
- `backend/gaia/runtimes/**`;
- `backend/gaia/services/proc.py` (add `stream_proc` and its helpers only);
- `backend/gaia/services/git.py` (add `changed_paths` only);
- `backend/gaia/cli.py` (`dev run-task` only);
- `backend/gaia/config/settings.py` (only if needed);
- `backend/tests/**`;
- a "Decisions and deviations (as built)" section at the end of
  `docs/implementation/10-agent-runtimes.md`, marked "10a".

**You must not:**
- edit other docs, `.github/`, `.pre-commit-config.yaml`, `[tool.*]` in `pyproject.toml`, `uv.lock`,
  `gaia/llm/**`, `gaia/security/**` or `gaia/api/**`, or add migrations;
- add dependencies;
- build a real runtime, the graph (12) or HTTP routes (14).

## 4. Non-negotiable rules
- Only `gaia/services/proc.py` spawns processes (ruff `TID251`). No shell.
- Never `os.environ.copy()`. A test proves the agent env is built from scratch.
- No secrets in code, tests, logs or events. A fake planted key in a test is fine if it is
  obviously fake.
- Every wait has a bound (timeouts, reaps). Tests must not leave processes or tasks behind.
  `warnings` are errors.
- **Library APIs:** stdlib `asyncio.timeout`, `asyncio.create_subprocess_exec` (in `proc.py` only),
  `os.killpg`. Confirm behaviour against Python 3.12 docs as installed.

## 5. How to work
1. Plan first: the files, the public API with signatures, and how each contract test is proven.
2. Build in this order: `base.py`, then `support.py` (env, files_changed), then `stream_proc`, then
   the orphans, then the brokers, then the fakes, then the contract suite, then the CLI.
3. Before you finish, all of these must pass, run from `backend/`: `uv run ruff check .`,
   `uv run ruff format --check .`, `uv run mypy` and `uv run pytest --cov -q` (100% coverage of
   `gaia/`). Process tests may take a few seconds. Keep the whole suite under 90 s.
4. Write the "Decisions and deviations (as built)" section.

## 6. When blocked
Stop and put the question under **Open questions** in your report, with what you have so far.

## 7. Report (the `finish` tool's `report` argument), in markdown
```
## Summary
## Plan vs. result
## Shared requirements 1-11   (each: met here / deferred to 10b, with test names)
## Contract suite   (test → what it proves, per runtime)
## Deviations from the doc
## External APIs used
## Security-relevant changes
## Tests added
## TODOs left for other packages
## Open questions
## Final check output   (last lines of ruff, mypy, pytest --cov)
```
