# 05b — Git hardening and review follow-ups

**Origin:** the 2026-11-13 code review ([`../reviews/2026-11-13-code-review-01-05.md`](../reviews/2026-11-13-code-review-01-05.md)),
findings **F1** (blocking), **F2**, **F4** and **F6**. Must merge before 06, 09a and 12a.
**Owner:** junior · lead review (security-critical).

## Goal
Close the planted-gitdir hole (F1) and the process-group escape (F2), add the approval
idempotency key that doc 12 needs (F4), and make request sessions commit before the response
is sent (F6).

## Out of scope
The OS sandbox (09a). Event publishing (13). Approval upsert helpers and graph logic (12a).
Composite cross-workspace foreign keys (F5, before multi-user).

## Files
- `gaia/services/git.py`, `gaia/services/worktrees.py`, `gaia/services/proc.py`
- `gaia/db/models.py`, `gaia/domain/schemas.py`, `alembic/versions/0002_*.py`
- `gaia/api/deps.py`, `gaia/api/errors.py`
- `backend/tests/**`
- this doc's "Decisions and deviations (as built)" section

## Tasks

### 1. F1: worktree git calls never trust the worktree's `.git` file
A worktree's `.git` is a file (`gitdir: <admin dir>`) inside the worktree, so an agent can
overwrite it. Git then reads the attacker's config, and options such as `core.fsmonitor` and
filter drivers execute commands in Gaia's **unsandboxed** git. Fix it as follows:

1. Add `WorktreeLocation`, a frozen dataclass in `git.py` with `work_tree: Path` and `git_dir: Path`
   (both resolved).
2. Add `GitService.worktree_admin_dir(repo: Path, work_tree: Path) -> Path`. Ask the **repository**
   (trusted) for `git rev-parse --git-common-dir`, then scan `<common>/worktrees/*/gitdir`. Each such
   file holds the path of a worktree's `.git` file. Return the directory whose entry resolves to
   `<work_tree>/.git`. If none matches, raise `UnsafePath`. Never read the worktree's own `.git`
   file to find the admin dir.
3. Every `GitService` method that runs *in* a working tree (`current_sha`, `current_branch`,
   `status_porcelain`, `is_dirty`, `diff`, `log`, `commit`) accepts `repo: Path | WorktreeLocation`.
   - For a `WorktreeLocation`, before running: check that `<work_tree>/.git` is a regular file (not a
     symlink) whose content is exactly `gitdir: <git_dir>` (with or without a trailing newline).
     Otherwise raise a new `WorktreeTampered(UnsafePath)`. Then run with `cwd=work_tree` and
     `GIT_DIR=<git_dir>`, `GIT_WORK_TREE=<work_tree>` in the env.
   - For a `Path` that resolves inside `worktrees_root`, raise `UnsafePath` ("pass a
     WorktreeLocation"), so no caller can forget the pin.
   - A `Path` outside `worktrees_root` (the owner's registered repository) behaves as today.
4. Add `-c core.fsmonitor=false` to `HARDENED_CONFIG`, and `--no-ext-diff --no-textconv` to `diff`.
   These are defence in depth only; the pin is the fix.
5. `Worktree` gets a nullable `git_dir` column (`String(PATH)`). `WorktreeService.create()` fills it
   (via `worktree_admin_dir`) after `git worktree add` succeeds. Add
   `WorktreeService.location(worktree) -> WorktreeLocation`, which uses the row's `git_dir`, or
   derives it with `worktree_admin_dir` for rows that predate the column. `remove()` checks
   dirtiness through `location()`. A `WorktreeTampered` during `remove()` propagates (never
   auto-force).

### 2. F2: a process that leaves its group cannot outlive the timeout
After `_kill_process_group` (on timeout or cancellation), `run_proc` must not wait without limit.
A grandchild that called `setsid()` keeps the pipes open, and `process.wait()` waits for the pipes.
- Wait at most `KILL_GRACE_S = 5.0` for the process and both readers to finish. If they haven't, stop
  reading: close the pipe transports, cancel the reader tasks, and keep the output read so far.
- `ProcResult` gains `escaped: bool = False` (the last field, with a default), set when the grace
  period ran out. Log a `proc.escaped` warning (program name and pid only).
- The leader's exit code is still reported. `process.returncode` is set as soon as the leader is
  reaped, even while the pipes are open.
- If you need an asyncio attribute that isn't public API, use the narrowest one, explain why in a
  comment, and test it. Tests run with warnings as errors, so no "unclosed transport" warnings are
  allowed.

### 3. F4: the approval idempotency key
Doc 12 upserts approval rows because a node re-runs from its first line on resume. Add
`approval.idempotency_key` (`String(200)`, not null) with `UNIQUE(run_id, idempotency_key)`. Graph
gates will use `"<node>:<step_index>:<attempt>"`, and in-turn command approvals
`"command:<tool_call_id>"` (document both conventions in the model docstring). Add it to
`ApprovalCreate` and `ApprovalRead`.

### 4. One migration
`alembic/versions/0002_*.py` (revision `0002`, revises `0001`) adds `worktree.git_dir` and
`approval.idempotency_key` with its unique constraint, using batch mode. Existing approval rows are
backfilled with their own `id` **before** the column becomes non-null. Generate it with
`uv run alembic upgrade head && uv run alembic revision --autogenerate -m "..." --rev-id 0002`
(`GAIA_DATA_DIR` points at a temp dir), then review it by hand and add the backfill. The downgrade
reverses it.

### 5. F6: commit before the response
- `get_session` is used with `Depends(get_session, scope="function")` (FastAPI ≥ 0.121; the repo has
  0.162.5), in `SessionDep` and in `get_scoped`, so the commit runs before the response is sent.
- Add a handler in `errors.py` that maps `sqlalchemy.exc.IntegrityError` to **409** `conflict` with a
  generic message. Never include the SQL or the parameters.
- Update the docstrings that describe the old after-response behaviour (`deps.py` module docstring).

## Tests
- F1 regression tests, built like `tests/git_repo.py`: create a worktree through `WorktreeService`,
  then (as an agent would) replace its `.git` with `gitdir: <planted dir>`, where the planted dir is a
  full copy of the repo's `.git` whose config sets `core.fsmonitor` to a command that creates a marker
  file. Repeat with a `[filter "x"] clean = ...` plus a `.gitattributes` of `* filter=x`.
  1. Through `WorktreeService.location()`, `is_dirty`/`commit` raise `WorktreeTampered` and the marker
     does not exist.
  2. With the `.git` file left intact but a planted `.gitattributes` referencing an undefined filter,
     calls succeed and nothing executes.
  3. `GitService.is_dirty(<path inside worktrees_root>)` raises `UnsafePath`.
  4. `worktree_admin_dir` finds the right dir and refuses an unknown path.
  5. `create()` stores `git_dir`; `location()` works for a row with `git_dir=None`.
- F2: `["python3", "-c", "import os,time; os.setsid(); time.sleep(30)"]` launched in the background
  by `sh -c '... & echo started'` with `timeout_s=1` returns in under `1 + KILL_GRACE_S + 2` seconds,
  with `timed_out=True`, `escaped=True` and stdout containing `started`. Make sure the test kills its
  escaped sleeper afterwards (find it by a unique argv marker).
- F4: duplicate `(run_id, idempotency_key)` raises `IntegrityError`; the same key on two runs is allowed;
  `test_migrations.py` still passes (upgrade, no drift, downgrade).
- F6: mount a test-only route that inserts a duplicate unique row. The client gets 409 with the
  envelope, and no SQL in the body.

## Acceptance criteria
- [ ] Both F1 attack variants are regression tests that pass, and no marker file is ever created.
- [ ] No `GitService` call can run in a worktree without a `WorktreeLocation`.
- [ ] The F2 test passes in bounded time with no warnings.
- [ ] Migration `0002` upgrades and downgrades cleanly, and `alembic check` shows no drift.
- [ ] `uv run pytest`, `uv run mypy` and `uv run ruff check . && uv run ruff format --check .` pass,
      with 100% coverage of `gaia/`.

## Decisions and deviations (as built)

1. **`WorktreeLocation` resolves both paths in `__post_init__`.** The task says both
   fields are resolved; enforcing it on construction means `_command_context` can
   compare strings directly and tests can assert with `Path.resolve()`.
2. **`WorktreeService.location` is async and re-fetches the repository for legacy rows.**
   The row's `git_dir` is authoritative when present. For a pre-0002 row it has to
   run `git rev-parse --git-common-dir` (async) and the repo path is needed for that
   scan; `_location_for(worktree, repo_path)` keeps `remove()` from fetching and
   validating the repo twice.
3. **The F1 regression test covers both attack variants with one parametrized test**
   (`core.fsmonitor` and `filter.x.clean` plus `.gitattributes`). It plants a full
   copy of the repo's `.git` under the worktree parent, then asserts both
   `is_dirty` and `commit` raise `WorktreeTampered` before the marker is created.
4. **The existing worktree test that called `current_branch(path)` changed to use
   `WorktreeService.location(worktree)`.** This is the deliberate behaviour change of
   F1: a bare path inside `worktrees_root` is now refused, so the test had to pass
   the new pin.
5. **`run_proc` keeps partial output by making `_read_capped` return on cancellation.**
   After the kill grace period, `run_proc` closes the pipe transports (using the
   narrow private `StreamReader._transport`), cancels the readers and then awaits
   them; `_read_capped` catches `CancelledError` and returns the bytes read so far.
   The private attribute is the only way asyncio exposes the transport to close, and
   the comment in `_close_stream_reader` says why.
6. **The F6 test does not flush in the route.** It adds a duplicate `users` row with
   `session.add` and returns, so the unique violation is raised by `get_session`'s
   teardown commit, not by the route body. That proves the commit now runs before
   the response.
7. **Migration `0002` was generated as `0002_worktree_git_dir_and_approval_.py`**
   (the autogenerate command adds a trailing underscore after the final word). It
   adds `approval.idempotency_key` as nullable, backfills `idempotency_key = id`,
   then makes it non-null and adds the unique constraint; `worktree.git_dir` stays
   nullable because existing rows have no safe value to backfill.
8. **No event/audit row was added for `WorktreeTampered`.** The task text says only
   to raise; event publishing is out of scope (doc 13), and a service layer raise is
   what the tests and doc 09b can observe.
