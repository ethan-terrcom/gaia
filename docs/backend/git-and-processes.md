# Git & processes

> Package 05 · Source of truth: [`docs/implementation/05-git-worktree-service.md`](../implementation/05-git-worktree-service.md)
> · Concepts used: [async and await](python-concepts.md#async-and-await),
> [process groups and signals](python-concepts.md#process-groups-and-signals),
> [git worktrees](python-concepts.md#git-worktrees), [dataclasses](python-concepts.md#dataclasses-and-frozen-objects)

## What it's for

Agents work in git. This layer gives each ticket its own **worktree** on a `gaia/...` branch, and
runs every git command, and later every agent command, in a way that a hostile repository can't
abuse. It has three parts, each building on the one before:

```
WorktreeService  (policy: which path, which row, what to refuse)
   └ GitService  (vocabulary: every git verb, in a hardened environment)
      └ run_proc (mechanics: no shell, minimal env, timeout, bounded output)
```

## Files

| File | Contents |
|---|---|
| `gaia/services/proc.py` | `run_proc()`, `ProcResult`, `base_environment()`, `ProcError` |
| `gaia/services/git.py` | `GitService`, `GitError` and friends, `resolve_within()`, `slugify()`, `short_id()` |
| `gaia/services/worktrees.py` | `WorktreeService`, `WorktreeReport`, `WorktreeItem`, the worktree errors |

## How it works

### `run_proc`: the only way to start a process

```python
result = await run_proc(["git", "status"], cwd=repo, timeout_s=30.0)
if not result.ok:
    ...  # result.returncode, result.stderr, result.timed_out
```

- **No shell.** `argv` must be a list of strings; a `str` raises `TypeError`. A ticket title or a
  model's output can only ever be an argument, never a command.
- **Allowlisted environment.** The child gets `PATH`, `HOME`, `LANG` and `GIT_TERMINAL_PROMPT=0`,
  plus whatever the caller passes in `env=`. Nothing else is inherited, so API keys in Gaia's own
  environment never reach a child.
- **Its own process group**, so a timeout `SIGKILL`s the whole tree and reaps it (no zombies).
  Cancelling the calling task does the same.
- **Bounded output.** Each stream keeps the first `max_output_bytes` (64 KB by default) and keeps
  draining the rest, so a chatty child never blocks; `stdout_truncated` says whether anything was cut.
- **Failure is data.** A command that ran and exited non-zero returns a result. Only "couldn't start
  it" raises `ProcError`.

### `GitService`: hardened git

Every call is `git <HARDENED_CONFIG> <subcommand>` with `GIT_CONFIG_NOSYSTEM=1` and
`GIT_CONFIG_GLOBAL=~/.gaia/gitconfig`. Your own `~/.gitconfig` and `/etc/gitconfig` are never read.
The `-c` flags, which beat any config file:

| Flag | Why |
|---|---|
| `core.hooksPath=/dev/null` (and `commit --no-verify`) | No repo hook, husky directory or agent-written hook ever runs |
| `commit.gpgsign=false` | Signing would launch a pinentry program and hang |
| `core.pager=cat`, `color.ui=never`, `core.quotepath=false` | Output is parsed, so no pager, no colour codes, no escaped paths |
| `user.name=Gaia`, `user.email=gaia@localhost` | A repo can't change who Gaia commits as |

Every ref is checked by `check_ref_argument()` (no leading `-`, so it can't become an option, and no
whitespace or control characters). Branch names are validated by `git check-ref-format`, and paths
come after `--`. `GitError` messages are redacted.

The main methods are `validate_repo`, `current_sha`, `default_branch`, `status_porcelain`,
`is_dirty`, `diff`, `log`, `commit`, `branch_name_for`, `validate_ref`, `branch_exists` and the
`worktree_*` verbs. Each takes `timeout_s=` for that one call.

### `WorktreeService`: one worktree per ticket

```python
service = WorktreeService(git=git, session=session, workspace_id=ws_id)
wt = await service.create(repo, ticket)        # row 'preparing' → git worktree add → 'ready'
report = await service.list(repo)              # rows vs `git worktree list`, both directions
await service.remove(wt)                       # refuses a dirty tree unless force=True
url = service.open_in_editor(wt)               # "vscode://file/..." (a string, never an action)
```

- Paths are always `<worktrees_root>/<repo_id>/<worktree_id>`, built from Gaia's own ids (checked
  to be a single safe path segment) and confined under the root with `resolve_within()`. No caller
  ever supplies a path.
- Branches are always `gaia/<last 8 chars of the ticket id>-<slugified title>`.
- The database row is written **before** the directory, so a crash leaves a row that says
  `preparing` rather than an unexplained directory. `remove()` keeps the row (marked `removed`) as
  history, and `create()` revives it for the same branch.
- `list()` reports **orphans** (rows git doesn't know) and **unknown paths** (worktrees no row
  claims), so the UI can offer cleanup.

## Why it's built this way

- **Repository content is untrusted.** A branch an agent worked on can contain anything, including
  hooks and git config. Hence the hardened config, no inherited environment, and no hooks.
- **`git.py` vs `worktrees.py`.** All argv building lives in `git.py`. `worktrees.py` decides
  policy and never touches argv. One place to audit for command construction.
- **No per-row dirty flag in `list()`.** It would cost one `git status` per worktree on every listing.
  The route asks only for the worktrees it shows.

## Changing it safely

- **Need a new git command?** Add a method to `GitService` that goes through `_run`/`_invoke`, and
  check every ref argument. Never call `run_proc(["git", ...])` from anywhere else; it would skip the
  hardening.
- **Need to run something that isn't git?** Use `run_proc`. If you need output as it streams, that's
  `stream_proc`, planned for doc 10. Add it to `proc.py` and nowhere else.
- **Open security findings (review 2026-11-13):**
  - **F1, blocking.** Git commands run *inside a worktree* still discover the git directory through the
    worktree's own `.git` file, which an agent can overwrite. That lets planted config execute
    commands. The fix pins `GIT_DIR`/`GIT_WORK_TREE` from the trusted repository side, and must land
    before agents write to worktrees.
  - **F2.** A child that calls `setsid()` escapes the process-group kill and the timeout. Fix it before
    agent commands use `run_proc` (09b).
- **Tests** build real throwaway repos with `tests/git_repo.py` (`make_repo()`, `commit_file()`),
  isolated from your own git config. Reach for those rather than mocking git.
