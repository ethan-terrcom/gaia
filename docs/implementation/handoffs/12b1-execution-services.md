# Handoff: 12b-1 — Real execution services for the graph (worktree, steps, checks, commits)

Filled from `HANDOFF_TEMPLATE.md` for the lead's sandboxed DeepSeek harness (2026-11-14). Your file
tools are confined to the worktree. Your `run` tool is sandboxed: no network, no git writes, no
installs. The lead commits and merges; you never commit. Your report goes through `finish`.

---

You are a junior engineer on **Gaia**, a local-first control plane for AI coding agents. The
orchestrator graph (12a) runs on **protocols** (`gaia/orchestrator/deps.py`), satisfied by fakes
in tests. Your package, **12b-1**, writes the **real implementations** of the execution half:
- `WorktreePreparer`: create or reuse the run's worktree and run the repo's setup command;
- `StepRunner`: run a plan step with an agent runtime;
- `CheckRunner`: run the step's test command in the sandbox;
- `Committer`: commit an accepted step.

It also adds the **role → runtime settings** (ADR-001 **P2**). The lead side (planner and
reviewer) is **12b-2**, built in parallel by another junior. Wiring everything into the app is
12b-3. Build neither.

## 1. Read first, in this order
1. `docs/implementation/12-orchestrator-graph.md`: tasks 2–4, and the whole 12a "Decisions and
   deviations" section, including the node → status table and the idempotency keys.
2. `docs/decisions/ADR-001-agent-runtimes.md`: **Portability P1–P6**.
3. `docs/implementation/00-README.md`, "Rules for every work package".
4. `docs/backend/orchestrator.md`, `runtimes.md`, `tools.md`, `git-and-processes.md`,
   `security.md`.
5. `docs/reviews/2026-11-14-review-12a.md`, `…-review-09b.md`, `…-review-10b.md`,
   `…-review-10c.md`. The bug classes there (the caller's task registered as "mine", boundary
   files inside the boundary, network inside a transaction) apply to your code too.
6. Code:
   - `gaia/orchestrator/{deps,graph,guard,runner,state}.py` and `nodes/*.py`;
   - `gaia/runtimes/{base,support,native,claude_agent_sdk,finish}.py`;
   - `gaia/tools/{registry,shell}.py`;
   - `gaia/services/{git,worktrees,events}.py`;
   - `gaia/security/{commands,sandbox}.py`;
   - `gaia/domain/{enums,plans,state}.py`, `gaia/db/{models,repo}.py`, `gaia/config/settings.py`;
   - `tests/test_orchestrator_*.py` (the fakes and fixtures 12a uses), `tests/git_repo.py`,
     `tests/runtime_contract.py`, `tests/tool_fakes.py`.

## 2. Lead notes for this package
These are design decisions already made. Follow them, and record any deviation in your report.

Put the implementations in a new package, `gaia/orchestrator/impl/`, one module per protocol.
Each class takes its collaborators in `__init__` (keyword-only), so 12b-3 can wire it and tests
can inject fakes. **No module-level state.**

### 2.1 Settings (P2): `gaia/config/settings.py`
Add the following, each with a docstring comment like its neighbours:
- `junior_runtime: RuntimeKind = RuntimeKind.NATIVE_DEEPSEEK`;
- `lead_runtime: RuntimeKind = RuntimeKind.CLAUDE_AGENT_SDK`;
- `junior_model: str = "deepseek-v4-pro"`. `lead_model` **already exists** (package 11); reuse
  it, don't add it again;
- `lead_attempt_from: int = 3` (≥ 0). From this attempt on, a step runs on the **lead** runtime;
  0 disables it. This is how ADR-001's "lead implementation and escalations" happens without a
  graph change: two junior attempts, then the lead takes the third, before the human escalation
  gate;
- `step_timeout_s: float = 1800`, `junior_max_turns: int = 60`, `lead_max_turns: int = 60`;
- `check_timeout_s: float = 900`, `setup_timeout_s: float = 900`;
- `setup_allowed_domains: tuple[str, ...]`, defaulting to the package registries a setup command
  needs: `("pypi.org", "files.pythonhosted.org", "registry.npmjs.org", "github.com",
  "objects.githubusercontent.com", "crates.io", "static.crates.io", "proxy.golang.org")`.

**Nothing in `gaia/orchestrator/` may name a vendor or a model** (P2). The impls read the role's
runtime and model from settings.

The registry key for a runtime is the `RuntimeKind` value: `native_deepseek` or
`claude_agent_sdk`. `NativeRuntime(name=...)` defaults to `"native"`, so 12b-3 passes
`name="native_deepseek"`; `ClaudeAgentSdkRuntime` already defaults to `"claude_agent_sdk"`. You
look them up with `registry.get(kind.value)`.

### 2.2 `impl/worktrees.py`: `WorktreeSetup` (implements `WorktreePreparer`)
`async def prepare(self, run_id: str) -> str` returns the worktree id. It is idempotent: a
resumed node calls it again.
1. **Load** (one short session): the run, ticket and repository.
   - If `run.worktree_id` is set and that row is `ready` or `in_use`, and `verify_location` passes,
     return it. Skip setup if it already ran: track this with a marker file under
     `<data_dir>/runs/<run_id>/setup.ok`, outside the worktree, so the agent cannot fake it.
   - Otherwise create it with `WorktreeService.create(repo, ticket)`. On `WorktreeAlreadyExists`,
     reuse `exc.worktree` when it is `ready` or `in_use`.
2. **F7, a failed row wedges the branch** (open since the 01–05 review). `_row_for` revives only
   `removed` rows, so a `failed` row makes every later attempt raise. Change `_row_for` in
   `gaia/services/worktrees.py` to revive `failed` rows too. Before `worktree_add`:
   - run `git.worktree_prune(repo_path)`;
   - if the target directory exists and is **empty**, remove it (`rmdir`);
   - if it exists and is **not empty**, raise `WorktreeError("a previous attempt left files at
     <path>; remove them or the worktree row")`, and never delete files. Gaia does not delete
     what it cannot prove it made.

   Add tests in `tests/test_worktrees*.py`.
3. Mark the row `in_use` (check the `WorktreeStatus` transitions; add `ready → in_use` if it is
   missing, as a deviation).
4. **Setup command** (`repository.setup_command`, an argv list set by the owner):
   - if present and the marker is absent, run it through an injected `CommandRunner` built for
     setup, with `settings.setup_allowed_domains` as its network allowlist (2.6). Use
     `cwd=worktree` and `timeout_s=settings.setup_timeout_s`;
   - it is **not** policy-checked: it is the owner's own registered command, run before any agent
     touches the worktree. Say so in the docstring;
   - non-zero exit or a timeout raises `SetupFailed(returncode, tail)`, where `tail` is the last
     2,000 characters of combined output after `redact()`;
   - success writes the marker.
5. **Transactions:** no git or network call inside a DB session (lead lesson from 15a: load →
   work → persist). Change `nodes/prepare_worktree.py` so `deps.worktrees.prepare()` runs
   **before** the node opens its session.

### 2.3 `impl/steps.py`: `RuntimeStepRunner` (implements `StepRunner`)
`implement(run_id, step_index, attempt, feedback) -> StepOutcome`:
1. **Load:** the run, the step row (`spec_json` → `PlanStep`), the repo, and the worktree
   `WorktreeLocation` (via `WorktreeService.location`).
2. **Choose the runtime:**
   - if `settings.lead_attempt_from` is set and `attempt >= lead_attempt_from`, use
     `lead_runtime`, `AgentRole.LEAD` and `lead_max_turns`;
   - otherwise use `RuntimeKind(run.junior_runtime)` if set, else `settings.junior_runtime`, with
     `AgentRole.JUNIOR` and `junior_max_turns`.

   Look it up with `runtimes.get(kind.value)`. Log `step.runtime_chosen` with the kind and
   attempt.
3. `base_sha = await git.current_sha(location)`. Accepted steps are committed, so this makes
   `files_changed` and the diff per step.
4. `instructions = render_step_instructions(step, attempt=attempt, feedback=feedback)`: a pure
   function in `impl/steps.py`. It renders the step's title, instructions, files hint, test
   command and acceptance (read `PlanStep`'s fields). On attempt > 1 it adds a "Reviewer
   feedback" section with the feedback wrapped in `wrap_untrusted(...)` from
   `gaia/llm/untrusted.py`. The feedback is model output and must be treated as data. Plain
   Markdown, no vendor syntax.
5. Build the `AgentTask`:
   - a fresh uuid7 `task_id`, plus the run, step and workspace ids, and the role;
   - `worktree=location`, `base_sha`;
   - `timeout_s=settings.step_timeout_s` and the max turns from step 2;
   - `extra_allow` = the repo's `test_command` and the step's test command, when present, as
     tuples.
6. `result = await run_with_limits(runtime, task, BusEventSink(bus, workspace_id=…, run_id=…,
   …), git=git)`. Check `BusEventSink`'s signature.
7. `diff = await git.diff_since(location, base_sha)` (2.5).
8. Return `StepOutcome(status=result.status, summary=result.summary, files_changed=list(result.
   files_changed), diff=diff)`.

   A `budget_exceeded` status is returned as is: the next node's guard sees the spent budget and
   raises the budget interrupt. Document that flow in the docstring.

### 2.4 `impl/checks.py`: `SandboxCheckRunner` (implements `CheckRunner`)
`run(run_id, step_index) -> CheckOutcome`:
- The command is the step's `test_command` if set, else the repository's. With neither, return
  `CheckOutcome(passed=True, output="no test command configured")` and log it.
- `policy.evaluate(argv, extra_allow=(repo test command,))`. On `deny` or `ask`, return
  `passed=False`, `output="check command not allowed: <reason>"`. The command was validated at
  plan time (doc 11), so this is defence in depth. **Never ask a human during checks.**
- Run through an injected `CommandRunner` (production: `SrtCommandRunner` with **no network**)
  with `timeout_s=settings.check_timeout_s`, in a per-check scratch dir under
  `<data_dir>/runs/<run_id>/checks/<step>/<uuid>` that is removed after.
- `passed = returncode == 0 and not timed_out`. `output` is the last 20,000 characters of
  `exit=N`, stdout and stderr, matching doc 11's `TEST_OUTPUT_CAP`.
- Persist `run_step.result_summary`: `"passed: <argv>"`, or `"failed (exit N): <argv>"`, at most
  500 characters. 15a's PR body reads it.

### 2.5 `GitService.diff_since(location, base_sha, *, max_output_bytes=...) -> str`
This is the step's whole change, **including new files**:
- run `git add --intent-to-add --all`, then `git diff --no-ext-diff --no-textconv <base_sha>`,
  with `check_ref_argument(base_sha)`;
- intent-to-add is harmless here: the committer stages everything anyway;
- return stdout; a truncated output gets a `\n[diff truncated]` note.

Tests: a modified file and a new untracked file both appear, and a bad SHA is refused.

### 2.6 Runners
- Add a keyword `allowed_domains: tuple[str, ...] = ()` to `SrtCommandRunner.__init__` in
  `gaia/tools/shell.py`, passed into the `SandboxSpec`. That is the only change in `shell.py`.
  `run_command`'s production runner keeps `()` (no network).
- `setup_runner(settings, srt_path)` and `check_runner(settings, srt_path)` factory functions in
  `impl/__init__.py` return the two configured runners. 12b-3 calls them.

### 2.7 `impl/commits.py`: `WorktreeCommitter` (implements `Committer`)
`commit_step(run_id, step_index, title) -> str`:
- Load the worktree location.
- Message: `[gaia] <title>`, with the first line capped at 200 characters.
- `git.commit(location, message, author=GitAuthor(name="Gaia", email="gaia@localhost"))`.
- **Idempotent:** if git says "nothing to commit", return `current_sha`. That is the resumed-node
  case, where the commit already happened. Reuse the detection approach from
  `tools/git_tools.py`.

### 2.8 Testability
- Use a real git repo and real `GitService`/`WorktreeService` (`tests/git_repo.py`), as the 10a
  contract tests do.
- Fake runtimes: `FakeRuntime` from `gaia/runtimes/fake.py`, registered in a `RuntimeRegistry`
  under `native_deepseek` and `claude_agent_sdk`, so a test can prove attempt 3 picks the lead.
- Fake command runners from `tests/tool_fakes.py`.
- **Integration test:** extend or copy 12a's graph happy-path test so the real `WorktreeSetup`,
  `RuntimeStepRunner` (with `FakeRuntime`s), `SandboxCheckRunner` (with a fake runner) and
  `WorktreeCommitter` run, with the planner and reviewer still faked, end to end through the
  graph. The run reaches `completed`, there is a commit per accepted step, and the PR is still the
  12a fake.

## 3. Files
**You may create:**
- `gaia/orchestrator/impl/{__init__,worktrees,steps,checks,commits}.py`;
- tests: `tests/test_impl_worktrees.py`, `test_impl_steps.py`, `test_impl_checks.py`,
  `test_impl_commits.py`, `test_orchestrator_real_execution.py`.

**You may edit:**
- `gaia/config/settings.py` (2.1), `gaia/services/git.py` (`diff_since` only),
  `gaia/services/worktrees.py` (F7 in `_row_for` and its caller only);
- `gaia/tools/shell.py` (`allowed_domains` only);
- `gaia/domain/state.py` (only if `ready → in_use` is missing);
- `gaia/orchestrator/nodes/prepare_worktree.py` (2.2 step 5);
- related existing tests;
- `docs/implementation/12-orchestrator-graph.md`: add "Decisions and deviations (as built) —
  12b-1".

**You must not touch:**
- `pyproject.toml`, `uv.lock`, `[tool.*]`;
- `.github/`, migrations;
- `gaia/orchestrator/{deps,graph,guard,runner,lead}.py`;
- `gaia/runtimes/`, `gaia/llm/`, `gaia/api/`;
- other docs.

12b-2 (parallel) edits `gaia/llm/` and creates `gaia/orchestrator/impl/{planning,review}.py`.
Don't create those, and add your settings in one block, so the lead's merge is easy.

## 4. Non-negotiable rules
- **P2:** no vendor or model name in `gaia/orchestrator/`; read settings.
- **No git, network or process work inside a DB session.**
- Never delete files Gaia cannot prove it created (F7).
- Feedback and plan text from a model are data: wrap them with `wrap_untrusted` in instructions.
- No `subprocess`; no `shell=True`. SQL only in `gaia/db/` repositories.
- mypy strict; 100% coverage; no new `# type: ignore` or `# noqa` without a reason.

## 5. How to work
1. Read §1, then write a short plan as your first message.
2. Order: settings → `diff_since` → F7 → `WorktreeSetup` and the node change → runners →
   `RuntimeStepRunner` → `SandboxCheckRunner` → `WorktreeCommitter` → the integration test.
3. Checks (from `backend/`): `uv run ruff check .`, `uv run ruff format --check .`,
   `uv run mypy`, `uv run pytest -q` (100% coverage).

## 6. When blocked
Write the question under "Open questions", choose the most restrictive reasonable behaviour, and
keep going.

## 7. Report (pass to `finish`, in Markdown, with these headings)
Summary · Files changed · Deviations · External APIs used · Security-relevant changes ·
Portability (P1–P6) · Tests added · Open questions · Final check output.
