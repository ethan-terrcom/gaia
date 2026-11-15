---
name: land-junior-package
description: Run a DeepSeek junior on a Gaia package and land the result safely. It covers worktree setup, dispatching the sandboxed harness, committing from an agent-written worktree without trusting its git metadata, the PR, CI and the squash-merge to main. Use for every junior package.
---

# Dispatching and landing a junior package

## Rules
- Each package gets **its own branch cut from the current `main`**, and its PR targets `main`.
  Never stack PRs, and never commit on the main checkout.
- Merge only with the owner's standing permission for the session, and only after CI is green.

## 1. Set up
```sh
git worktree add -b impl/<NN>-<name> <scratch>/worktrees/<NN>-<name> main
cd <worktree>/backend && uv sync          # the sandbox is offline: install now
cd <worktree>/frontend && npm ci          # the pre-commit frontend hook needs node_modules
cp <handoff> <worktree>/docs/implementation/handoffs/
```
The lead adds any new dependency (`uv add 'pkg==X'`, pinned to a version at least two weeks old)
on the branch **before** dispatch.

## 2. Dispatch
The harness is a native DeepSeek tool loop:
- its file tools are confined to the worktree, and `.git` is unreachable;
- `run` is allowlisted to `uv run pytest|ruff|mypy|alembic|python|gaia` and read-only git;
- every command runs under `sandbox-exec`, with no network, writes only inside the worktree, and
  no reads of shell profiles, `~/.ssh`, `~/.gaia` or the keychains;
- the key comes from `DEEPSEEK_KEY`, is popped from the environment, and never reaches a child
  process.

The canonical copy is `backend/spikes/native_deepseek_loop/agent.py` on `main`. Run it from the
main checkout, never from a junior's worktree. The full guide is
`docs/operations/running-junior-agents.md`.
```sh
cd <repo>/backend/spikes/native_deepseek_loop
zsh -ic "exec uv run --no-project --with httpx python agent.py --worktree <wt> \
  --task <wt>/docs/implementation/handoffs/<NN>.md --log runs/<NN>.jsonl \
  --report runs/<NN>-report.md --max-turns 220"        # run in the background
```
You can run independent packages in parallel, one worktree each. Don't poll: wait for the
completion notification.

## 3. Commit without trusting the worktree
The agent could write anything inside the worktree, including a planted `.git` file (finding F1).
Before running any git command there:
```sh
test ! -L <wt>/.git && [ "$(cat <wt>/.git)" = "gitdir: <repo>/.git/worktrees/<name>" ]
export GIT_DIR=<repo>/.git/worktrees/<name> GIT_WORK_TREE=<wt>
git add -A && git status --short          # read the list: no junk, nothing outside scope
git commit -m "<NN>: <title>" -m "<what, lead fixes, review doc>" -m "Co-Authored-By: ..."
git push -u origin impl/<NN>-<name>
```
If the pre-commit hooks modify files, re-add the files and commit again. Never use `--no-verify`.

## 4. Open the PR, wait for CI, merge
- `gh pr create --base main` with a body containing: a summary, the lead fixes, the flagged items,
  live verification, external APIs used, and the review doc link.
- `gh pr checks <n> --watch`. If the network drops, run it again.
- `gh pr merge <n> --squash --delete-branch`.
- Then `git -C <repo> pull --ff-only` on main, `git worktree prune`, and delete the scratch worktree.
- When the next package depends on this one, cut its branch from the **updated** `main`.

## 4b. Live checks that need a provider key
Never put a real key in an agent's environment or in the Keychain on the owner's behalf. For lead-run
live checks, start the server (or call the CLI) through a small launcher. The launcher reads the key
with `zsh -ic` and loads it into an `InMemorySecretStore` inside that trusted process, then patches
`get_secret_store`. The agent side gets only the gateway URL and a `gaia_rt_` run token, and runs
under `env -i`. Afterwards, `grep -c` the server log for the key and the token: both must be 0.

## 5. After a parallel batch
Merge one PR at a time. Rebase the next branch on `main` (`git rebase main` with the pinned
`GIT_DIR`), resolve the conflicts (`AppServices`, `app.py` routers, migration revision chain), rerun
`make check`, push with `--force-with-lease`, then merge.

## Rebasing after a parallel batch (as done for 09b → 15a → 16)
- Merge one PR, then `git fetch origin && git rebase origin/main` on the next branch, using the
  pinned `GIT_DIR`.
- The usual conflicts are the index rows in `backend/README.md` and `docs/backend/README.md`, and
  neighbouring `Settings` validators. Rows: keep both sides. Validators: merge the field lists.
  The "What exists today" test count: rerun `pytest` and write the real number.
- Regenerate artefacts after a rebase (`make openapi`, `make graph`) in case the other branch
  changed the API or the graph, then run **all** checks, frontend included, before
  `git push --force-with-lease`.
