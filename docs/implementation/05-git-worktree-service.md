# 05 — Git & Worktree Service

**Status:** done (PR `impl/05-git-worktree-service`). The tasks below are the plan as written;
"Decisions and deviations as built" records what actually shipped.

## Goal
A safe service layer for git: register repos, and create, list and remove worktrees and
branches for runs.

## Out of scope
GitHub API calls (15) and pushing to the remote (15 owns `push`).

## Files
- `gaia/services/proc.py`: `run_proc()`, `ProcResult`, `base_environment()`, `ProcError`. The
  **only** module that may spawn a process; ruff's `TID251` ignore names this exact path.
- `gaia/services/git.py`: `GitService` (every git verb, the hardened environment),
  `GitError`/`NotARepository`/`InvalidRef`/`UnsafePath`, `resolve_within()`, `Commit`,
  `GitWorktree`, `slugify()`, `short_id()`.
- `gaia/services/worktrees.py`: `WorktreeService` (`create`, `remove`, `list`, `open_in_editor`),
  `WorktreeReport`/`WorktreeItem`, `WorktreeError`/`WorktreeAlreadyExists`/`WorktreeDirty`.
- `backend/tests/test_proc.py`, `test_git_service.py`, `test_worktree_service.py`: the doc's tests.
- `backend/tests/git_repo.py` (new): `make_repo()`, `commit_file()`, `git()`, `resolved()`,
  `on_disk()` - the `tmp_path` repository helpers, run with their own git environment.
- `backend/tests/conftest.py`: the `git_repo`, `git_settings` and `git_service` fixtures.

`gaia/cli.py` was touched only to re-point one `TODO` at doc 16; see deviation 9 about
`gaia ui --open`.

## API (as built)

| Call | Returns |
|---|---|
| `await run_proc(argv, cwd, timeout_s=30.0, env=None, max_output_bytes=65536)` | `ProcResult` |
| `GitService(settings, timeout_s=30.0)` | - (writes `<data_dir>/gitconfig` here) |
| `await git.validate_repo(path)` | the repo root as a resolved `Path` |
| `await git.current_sha(repo, rev="HEAD")` / `current_branch(repo)` / `default_branch(repo)` | `str` / `str \| None` / `str` |
| `await git.status_porcelain(repo)` / `await git.is_dirty(repo)` | `str` / `bool` |
| `await git.diff(repo, base, head, max_output_bytes=…)` | `str` |
| `await git.log(repo, limit=20, rev="HEAD")` | `list[Commit]` |
| `await git.commit(repo, message, add_all=True)` | the new SHA |
| `git.branch_name_for(ticket)` | `gaia/<id-tail>-<slug>` |
| `await git.validate_ref(name)` / `await git.branch_exists(repo, branch)` | `str` / `bool` |
| `await git.worktree_list(repo)` | `list[GitWorktree]` |
| `await git.worktree_add(repo, path=…, ref=…, new_branch=None)` | - |
| `await git.worktree_remove(repo, path, force=False)` / `worktree_prune(repo)` | - |
| `WorktreeService(git=…, session=…, workspace_id=…)` | - (scoped like `ScopedRepo`) |
| `await service.create(repo, ticket, base_ref=None)` | the `worktree` row, `ready` |
| `await service.remove(worktree, force=False)` | the row, now `removed` |
| `await service.list(repo, include_removed=False)` | `WorktreeReport` |
| `service.open_in_editor(worktree)` | `vscode://file/<path>` |

Every `GitService` command method also takes `timeout_s=` to bound that one call (deviation 12),
so `git.log(repo, limit=5, timeout_s=5.0)` and `git.diff(repo, base, head, timeout_s=60.0)` both
work.

`ProcResult` carries `argv`, `cwd`, `pid`, `returncode`, `stdout`, `stderr`, `stdout_truncated`,
`stderr_truncated`, `duration_s` and `timed_out`, plus the `ok`/`truncated` properties and
`describe()` for an error message.

## Tasks (as built)
1. `run_proc()`: `create_subprocess_exec` with `shell=False` by construction (a `str` argv is a
   `TypeError`, an empty one a `ProcError`), `start_new_session=True`, and `os.killpg(pid, SIGKILL)`
   on timeout followed by `wait()` so nothing is left unreaped. Each stream is drained in 32 KB
   chunks and the first `max_output_bytes` kept, so a chatty child never blocks on a full pipe.
   The child env is an allowlist: `PATH`, `HOME`, `LANG`, `GIT_TERMINAL_PROMPT=0`, plus whatever the
   caller passes. A command that ran and failed returns its exit code; only "could not start" raises.
2. `GitService(settings, timeout_s=30.0)`: the hardened env and the git config file as the doc
   specifies, plus the identity pinned with `-c user.name/-c user.email` and
   `color.ui=never`/`core.quotepath=false` for parsing (deviations 2 and 3). `validate_repo` returns
   the repo root and refuses anything under `worktrees_root`; `current_sha`, `current_branch`,
   `default_branch`, `status_porcelain`, `is_dirty`, `diff`, `log`, `commit`, `branch_name_for`,
   `validate_ref` (`git check-ref-format --branch`) and `branch_exists`. Every ref is checked for a
   leading `-` before it becomes an argument, so a revision cannot become an option, and every method
   takes `timeout_s=` (deviation 12).
3. `WorktreeService`: `create(repo, ticket, base_ref=None)` writes the row first (`preparing`), runs
   `git worktree add -b <branch> <root>/<repo_id>/<worktree_id> <base_ref>`, and only then marks it
   `ready` - or `failed` if git said no. `remove(worktree, force=False)` refuses a dirty tree, then
   `git worktree remove` + `git worktree prune`, and marks the row `removed` without deleting it.
   `list(repo, include_removed=False)` reconciles the rows with `git worktree list --porcelain` and
   reports orphans in both directions. `open_in_editor(worktree)` returns a `vscode://file/...` URL
   and does nothing else.
4. Paths: `resolve_within(root, path)` resolves both sides (symlinks included) and refuses anything
   outside; the worktree path is `worktrees_root` joined with Gaia-generated ids, each checked
   against `^[0-9a-zA-Z-]{1,64}$` before it becomes a directory name.


## Tests
`backend/tests/git_repo.py` builds the `tmp_path` repository (one commit on `main`, its own
`GIT_CONFIG_NOSYSTEM`/`GIT_CONFIG_GLOBAL`, so neither the owner's `~/.gitconfig` nor CI's matters).
- `test_worktree_service.py::test_create_makes_a_worktree_on_a_gaia_branch_under_the_root`, then
  `::test_remove_deletes_the_worktree_and_keeps_the_row`: create, list, remove.
- `::test_remove_refuses_a_dirty_worktree` raises `WorktreeDirty`;
  `::test_remove_with_force_discards_the_changes` discards it.
- `test_git_service.py::test_validate_ref_rejects_what_git_rejects[gaia/bad..ref]` and
  `[…-gaia/bad ref]` for the `..` and space cases.
- `test_proc.py::test_run_proc_times_out_and_leaves_no_zombie` (`sleep 10`, `SIGKILL`, pid gone) and
  `::test_run_proc_kills_the_whole_process_group` (a grandchild dies too).
- `test_proc.py::test_run_proc_does_not_inherit_the_parent_environment` (the doc's
  `ANTHROPIC_API_KEY` test) and `::test_base_environment_falls_back_when_the_parent_is_stripped`.
- `test_git_service.py::test_commit_does_not_run_a_repo_hook` (a `.git/hooks/pre-commit` that would
  exit 1) and `::test_commit_does_not_run_a_hooks_path_directory` (a husky-style `core.hooksPath`),
  plus `::test_commit_is_authored_by_gaia_not_by_the_repo` and
  `::test_commit_ignores_a_repo_that_turns_on_signing`.

Also covered: an unknown base ref leaves no row; a live worktree refuses a second `create`; a
`removed` row is revived on the same branch and then hidden from the default `list()`; a row whose
worktree vanished is an orphan; a worktree no row claims lands in `unknown_paths`; a row from another
workspace is refused; a row whose `path` escaped the root is refused; a git failure is recorded as
`failed`; `ProcError` for a missing binary and a missing directory; a token in argv or stderr is
redacted out of `GitError`; `test_git_service.py::test_a_per_command_timeout_overrides_the_service_default`
records what reaches `run_proc` for a default call and for `timeout_s=90.0`.

## Acceptance criteria
- [x] Every git call goes through `run_proc`. `grep -rn "subprocess" gaia/ | grep -v proc.py` is
      empty. Evidence: `grep -rn --include='*.py' subprocess gaia/ | grep -v services/proc.py`
      prints nothing (the only hit without `--include` is `proc.py`'s own byte code in
      `__pycache__`).
- [x] `make check` passes: 341 backend tests, 100% line and branch coverage of `gaia/`, mypy strict
      clean, gitleaks clean.

## Decisions and deviations (as built)

**1. The split between the two git modules.** The doc's task 3 puts `git worktree add/remove/prune`
in `WorktreeService`, but every git call must go through the hardened environment of task 2. So
`git.py` owns the *git vocabulary* - including `worktree_list`, `worktree_add`, `worktree_remove`,
`worktree_prune` and the `--porcelain` parser - and `worktrees.py` owns the *policy*: which path,
which row, which status, what to refuse. Nothing in `worktrees.py` builds an argv.

**2. The commit identity is pinned on the command line, not only in the config file.** Git's
precedence is system < global < repo-local < `-c`, so a repo that sets `user.email` beats
`GIT_CONFIG_GLOBAL` - and a repo's content is untrusted. `HARDENED_CONFIG` therefore also carries
`-c user.name=Gaia -c user.email=gaia@localhost`. A later `-c` on one call still wins, which is how
doc 09b will attribute a commit to the agent that made it.

**3. Three more `-c` entries than the doc lists.** `core.pager=cat` (nothing may decide to page us),
`color.ui=never` and `core.quotepath=false` (this output is parsed: ANSI escapes and octal-escaped
paths would otherwise have to be undone twice). `commit.gpgsign=false` is passed on every call as
well as written to the config file, because a signing attempt means spawning `pinentry` - and it
fails with no terminal.

**4. `commit()` is an addition.** The doc's tests require that a repo hook does not run "when Gaia
commits", and doc 09b's `git_commit` tool needs a hardened path to it, so `GitService.commit()` is
there: `git add --all` (unless `add_all=False`), then `git commit --no-verify`.

**5. `WorktreeService` is constructed like `ScopedRepo`:** `WorktreeService(git=…, session=…,
workspace_id=…)`. Services sit *below* `gaia.api`, so they cannot take doc 04's `Scoped`; passing the
workspace id and building `ScopedRepo`s internally keeps every write scoped and refuses a row from
another workspace with doc 03's `WorkspaceMismatch`.

**6. `create()` revives a `removed` row instead of inserting a second one.** `worktree` has a unique
`(repo_id, branch)` constraint, and a run resumed after cleanup must not fail on a stale row. A live
worktree raises `WorktreeAlreadyExists`, whose `.worktree` is the row to reuse: doc 12's
`prepare_worktree` decides whether the old tree is worth keeping (it may hold uncommitted work).

**7. `list()` reports both directions, and hides `removed` rows by default.** `WorktreeReport.items`
has one `WorktreeItem` per row (`in_git`, `exists`, and `.orphan` = `not in_git`), and
`unknown_paths` holds worktrees git knows that no row claims. The repository's own working tree is
dropped from `unknown_paths`: it is in `git worktree list`, but it is not a run's worktree. A
`removed` row is history of where a run worked rather than something the repository has, so
`list()` leaves it out and `include_removed=True` is the audit view - otherwise every cleaned-up run
would still be listed by doc 06's route.

A per-row `dirty` flag is deliberately *not* in `list()`: it costs one `git status` per worktree,
and listing is one HTTP request, so doc 06's worktree route asks `GitService.is_dirty(item.path)`
for the worktrees it actually shows. `TODO(gaia-06)` sits on `WorktreeService.list`, and doc 17
shows the result.

**8. `run_proc` returns a result for a command that failed, and raises only when it cannot start
one.** The exit code is data (`git merge-base` uses 1 as an answer), and `GitError` is where
`GitService` decides a non-zero status is a failure. A timeout is a *result* with
`timed_out=True` and `returncode=-9` (SIGKILL). `ProcResult.pid` exists for the no-zombie test and
for log correlation. `GitError` redacts its whole message, argv included, because doc 15 will have a
token somewhere near a push.

**9. `gaia ui --open` was not implemented, and its `TODO(gaia-05)` was re-pointed at doc 16.**
Opening `http://127.0.0.1:5173/#token=<token>` hands the API token to a child process's argv, where
`ps` can see it - the very token doc 04 keeps out of URLs and SSE. Nothing in `proc.py` makes that
safer: the process *is* the leak. Doc 16 owns the login flow (a keychain read, or a one-time code
instead of the token), so `cli.py` says `TODO(gaia-16)` and its docstring now says "printed, not
opened" for that reason rather than because `run_proc` was missing.

**10. Paths.** Task 4's `security.paths.confine()` arrives with doc 09a, so `git.resolve_within()`
does the job here (resolve both sides, then `is_relative_to`). It is marked `TODO(gaia-09a)`, and 09a
should replace it rather than add a second copy.

**11. Left for the docs that own it.** `TODO(gaia-10)` in `proc.py` for `stream_proc` (the runtimes
need output as it arrives; every caller here wants a buffered result) and `TODO(gaia-15)` in
`git.py` for `push`, which is the only thing that needs a credential and belongs to the doc that
owns the remote.

**12. The timeout is per command, not only per service.** `GitService(settings, timeout_s=30.0)` sets
the default, and every command method takes `timeout_s=` to override it for one call. The right
bound is a property of the command - a `status` on a request path wants seconds, doc 15's `push` of
a large repo wants minutes - and threading a constructor argument through the callers would either
make every push wait 30 s or make every status wait 5 minutes. `_invoke`/`_run` already had the
parameter; the public methods now take it too, and
`test_a_per_command_timeout_overrides_the_service_default` records that 5.0 and 90.0 both reach
`run_proc`.
