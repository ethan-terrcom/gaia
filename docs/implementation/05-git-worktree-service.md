# 05 — Git & Worktree Service

## Goal
A safe service layer for git: register repos, and create, list and remove worktrees and
branches for runs.

## Out of scope
GitHub API calls (15) and pushing to the remote (15 owns `push`).

## Files
- `gaia/services/proc.py`: the **only** place that may call `asyncio.create_subprocess_exec`.
- `gaia/services/git.py`
- `gaia/services/worktrees.py`

## Tasks
1. `run_proc(argv: list[str], cwd: Path, timeout_s: float, env: dict[str,str] | None, max_output_bytes: int) -> ProcResult`
   - `shell=False` always. argv must be a list.
   - Kill the whole process group on timeout (`start_new_session=True`, then `os.killpg`).
   - Truncate stdout and stderr to `max_output_bytes`, and mark them as truncated.
   - The default env is minimal (`PATH`, `HOME`, `LANG`, `GIT_TERMINAL_PROMPT=0`). Never inherit the full parent env.
2. `GitService`:
   - `validate_repo(path)`: the path is a git repo and is not inside `worktrees_root`.
   - `current_sha`, `default_branch`, `status_porcelain`, `diff(base, head)`, `log`.
   - `branch_name_for(ticket)` → `gaia/<ticket-short-id>-<slug>`. Slug: lowercase `[a-z0-9-]`, at most 40 characters.
   - Validate ref names with `git check-ref-format --branch`.
   - **Hardened git environment for every call:** `GIT_CONFIG_NOSYSTEM=1`,
     `GIT_CONFIG_GLOBAL=<data_dir>/gitconfig` (Gaia-managed: `user.name`, `user.email`,
     `commit.gpgsign=false`), and `-c core.hooksPath=/dev/null` on every invocation; commits use
     `--no-verify`. Repo hooks can be tracked files (husky's `.husky/`) that an agent can write,
     so Gaia must never run them.
3. `WorktreeService`:
   - `create(repo, ticket, base_ref)` → `git worktree add -b <branch> <worktrees_root>/<repo_id>/<worktree_id> <base_ref>`.
     Record a `worktree` row with `base_sha`.
   - `remove(worktree, force=False)`: refuse if there are uncommitted changes unless `force`.
     Then run `git worktree prune`.
   - `list(repo)`: reconcile DB rows against `git worktree list --porcelain` and flag orphans.
   - `open_in_editor(worktree)`: return a `vscode://file/<path>` URL. **Do not** execute anything.
4. Every path argument goes through `Path.resolve()` and must stay under `worktrees_root` or a
   registered repo path.

## Tests
Use a `tmp_path` git repo fixture (run `git init` and commit a file).
- Create a worktree, then list it, then remove it.
- Removing a dirty worktree without force raises.
- A branch name with `..` or spaces is rejected.
- `run_proc` times out a `sleep 10` and leaves no zombie process.
- The env passed to the child does not contain `ANTHROPIC_API_KEY` even when it is set in the parent.
- A repo `pre-commit` hook, including one under a `core.hooksPath` directory, does not run when Gaia commits.

## Acceptance criteria
- [ ] Every git call goes through `run_proc`. `grep -rn "subprocess" gaia/ | grep -v proc.py` is empty.
