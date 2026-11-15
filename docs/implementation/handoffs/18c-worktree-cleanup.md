# Handoff: 18c-cleanup — Clean up worktrees and scratch dirs of finished runs

Filled from `HANDOFF_TEMPLATE.md` for the lead's sandboxed DeepSeek harness (2026-11-14). Your file
tools are confined to the worktree. Your `run` tool is sandboxed: no network, no git writes, no
installs. The lead commits and merges; your report goes through `finish`.

---

You are a junior engineer on **Gaia**. Your task is the cleanup part of package **18c**. Each run
leaves behind:
- a git worktree, a full checkout, under `{data_dir}/worktrees/`;
- scratch directories under `{data_dir}/runs/<run_id>/`: the setup marker and temp dirs, check
  scratch dirs, and task dirs.

Left alone, these fill the disk. Remove them for runs that finished a while ago, **without ever
destroying work a human might want**.

## 1. Read first
1. `docs/implementation/18-budgets-observability-hardening.md` (18c) and
   `docs/implementation/05-git-worktree-service.md` (the worktree lifecycle).
2. `docs/implementation/00-README.md`, "Rules for every work package".
3. `docs/backend/git-and-processes.md`, `orchestrator.md`, `github.md`.
4. Code:
   - `gaia/services/{worktrees,git}.py` (`WorktreeService.remove`, `list`, `location`; the
     `WorktreeStatus` values);
   - `gaia/services/pull_requests.py` (the `TODO(gaia-18c)` after a merge);
   - `gaia/orchestrator/impl/{worktrees,checks}.py` (where the `runs/<run_id>/…` dirs are made);
   - `gaia/db/{models,repo}.py` (`Run.finished_at`, `Run.status`, `Worktree`),
     `gaia/domain/enums.py`;
   - `gaia/api/app.py` (the lifespan's `background_tasks`; 15b's `PRSyncService` is the pattern
     for a periodic task), `gaia/cli.py`, `gaia/config/settings.py`;
   - `tests/git_repo.py`.

## 2. Lead notes
These are design decisions already made. Follow them, and record any deviation in your report.

### 2.1 `gaia/services/cleanup.py`: `CleanupService`
- `CleanupService(*, sessions, git, settings, now: Callable[[], datetime] = utcnow)`.
- `async def plan(self, *, older_than_days: int) -> CleanupPlan` is **read-only** and does nothing
  to disk. It returns `CleanupPlan{worktrees: list[CleanupItem], scratch_dirs: list[Path],
  skipped: list[CleanupSkip]}`.
  - **A worktree is a candidate** only if **all** of these hold:
    - its row's status is `ready` or `in_use`;
    - **every** run that uses it (`Run.worktree_id == row.id`) is terminal: `completed`,
      `cancelled` or `failed`. Check the terminal set in `gaia/domain/state.py`;
    - the newest such run's `finished_at` is older than `older_than_days`. A terminal run with
      `finished_at` null counts as **not** old enough: skip it, reason `"no finish time"`;
    - its directory exists.
  - **Skipped, with a reason, never removed:**
    - a **dirty** worktree (`git.is_dirty(location)` true): reason `"uncommitted changes"`;
    - a tampered one (`WorktreeTampered`);
    - a worktree whose branch has **commits not on the base**. Use `git.log(location, base_sha..HEAD)`
      or the existing verb that lists them, unless its PR row is `merged`. An unmerged branch with
      work on it is kept; reason `"unmerged commits"`. The branch itself survives `worktree
      remove` anyway, but say so in the report.
  - **Scratch dirs:** `{data_dir}/runs/<run_id>/` for terminal runs older than the threshold.
    Scratch is Gaia's own, so dirty or not does not matter. Never follow symlinks (`os.scandir`,
    `follow_symlinks=False`), and only directories directly under `{data_dir}/runs/` whose name is
    a run id that exists in the DB **and** is terminal.
  - Load the rows in one short session, then run git outside it.
- `async def apply(self, plan: CleanupPlan) -> CleanupResult` removes exactly what `plan`
  returned:
  - worktrees via `WorktreeService.remove(row, force=False)`. **Never `force`**: a worktree that
    became dirty since planning makes `remove` refuse, which is a skip;
  - scratch dirs via `shutil.rmtree` after re-checking each one with `resolve_within(data_dir /
    "runs", path)` and `not path.is_symlink()`.

  It returns counts and skips. Every removal is logged (`cleanup.worktree_removed`,
  `cleanup.scratch_removed`) and written as an `audit_log` row (`actor="system"`,
  `action="cleanup.worktree"` or `"cleanup.scratch"`).

### 2.2 Entry points
- **CLI:** `gaia worktrees cleanup [--older-than DAYS] [--apply]`. **Dry run by default**: it
  prints the plan as a table (path, branch, last run status, finish date, and skip reasons) and
  removes nothing unless `--apply` is given. Follow `gaia/cli.py`'s argparse patterns; it opens
  the DB like the other commands.
- **Background:** settings `cleanup_enabled: bool = False`, `cleanup_older_than_days: int = 14`
  (≥ 1) and `cleanup_interval_s: float = 86400`. **Off by default**: deleting a user's checkouts
  on a timer is something they opt into. Record this as a deviation from doc 18's wording, for
  the owner to confirm. When enabled, the lifespan starts a task like `PRSyncService.run_forever`
  that runs `plan` then `apply`, logs failures, and never crashes the app. Put the flag in the
  shared test settings fixture as `False`; it is already the default.
- **After a merge**, replace the `TODO(gaia-18c)` in `PullRequestService.merge` with nothing. The
  periodic or CLI cleanup handles merged runs once they are old enough. Just remove the TODO and
  say so in the docstring.

### 2.3 Tests
Real git repos and worktrees (`tests/git_repo.py`), real `GitService` and `WorktreeService`.
- **Candidate selection:**
  - old completed → candidate;
  - recent → skipped (not listed);
  - an active run on the same worktree → not a candidate;
  - `finished_at` null → skip;
  - `failed` and `cancelled` count as terminal.
- **Dirty → skipped, and `apply` never touches it.** Make a worktree dirty **between** `plan` and
  `apply`, and assert `remove` refused, it is reported as skipped, and the files are intact.
- Unmerged commits → skipped; the same with a merged PR row → candidate.
- Scratch: removed for an old terminal run; a symlink planted at `runs/<id>` is not followed (the
  target dir survives); a name that is not a run id is ignored.
- Audit rows are written; the CLI dry run removes nothing; `--apply` removes; the background task
  runs one round and handles an exception (inject `sleep`, as 15b's tests do).

## 3. Files
**You may create:** `gaia/services/cleanup.py`; `tests/test_cleanup.py`, `tests/test_cli_cleanup.py`.

**You may edit:**
- `gaia/cli.py` (the `worktrees` command group);
- `gaia/config/settings.py` (one block, `# --- cleanup (18c) ---`);
- `gaia/api/app.py` (the lifespan task only);
- `tests/conftest.py` (the flag);
- `gaia/services/pull_requests.py` (remove the TODO only);
- `gaia/db/repo.py` (the queries);
- `docs/implementation/18-budgets-observability-hardening.md`: under the existing "18c (partial)"
  section, add "Worktree cleanup (as built)".

**You must not touch:**
- `pyproject.toml`, `uv.lock`, `[tool.*]`;
- `.github/`, migrations;
- `gaia/orchestrator/`, `gaia/runtimes/`, `gaia/tools/`, `gaia/api/routes/`;
- other docs.

Two other juniors (12b-3 wiring, 14 API) are editing `app.py`, `deps.py` and `settings.py` in
parallel; keep your edits there small and in one block.

## 4. Non-negotiable rules
- **Never delete work.** Never pass `force` to `remove`, and never remove a dirty, tampered or
  unmerged-work worktree. Never follow a symlink in a removal.
- Removing anything requires `--apply` (CLI) or the opt-in flag (background).
- SQL only in `gaia/db/`. No git inside a DB session.
- mypy strict; 100% coverage; no new `# type: ignore` or `# noqa` without a reason.

## 5. Checks (from `backend/`)
`uv run ruff check .` · `uv run ruff format --check .` · `uv run mypy` · `uv run pytest -q` (100%).

## 6. Report (pass to `finish`)
Summary · Files changed · Deviations · Security-relevant changes · Tests added · Open questions ·
Final check output.
