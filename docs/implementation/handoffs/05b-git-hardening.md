# Handoff: 05b — Git hardening and review follow-ups

Filled from `HANDOFF_TEMPLATE.md` for the lead's sandboxed DeepSeek harness (2026-11-13). The
harness gives the junior file tools confined to its worktree and a sandboxed `run` tool (no
network, no git writes, no package installs), so the git/PR steps of the template are done by the
lead. The report goes through `finish` instead of a PR description.

---

You are a junior engineer on **Gaia**, a local-first control plane for AI coding agents. Your task is
to implement exactly one work package, **05b — Git hardening and review follow-ups**. The lead
engineer reviews your work, then commits and merges it. You never commit.

## 1. Read first, in this order
1. `docs/implementation/05b-git-hardening.md`: your work package. Its Tasks, Tests and Acceptance criteria are your contract.
2. `docs/reviews/2026-11-13-code-review-01-05.md`: findings F1, F2, F4, F6 (the why).
3. `docs/implementation/00-README.md`: "Rules for every work package". Every rule applies.
4. `docs/implementation/05-git-worktree-service.md` and `04-api-skeleton-auth.md`: what exists, and why.
5. `docs/backend/git-and-processes.md`, `docs/backend/api.md`: orientation.
6. The code you will change: `backend/gaia/services/{proc,git,worktrees}.py`, `backend/gaia/db/models.py`,
   `backend/gaia/domain/schemas.py`, `backend/gaia/api/{deps,errors}.py`, `backend/alembic/`, and the
   existing tests for them (`backend/tests/test_{proc,git_service,worktree_service,api_*,migrations}.py`,
   `tests/git_repo.py`, `tests/conftest.py`). Read them; do not guess their API.

Lead notes for this package:
- The lead reproduced F1 by hand: a worktree `.git` file rewritten to `gitdir: <full copy of the repo's .git with extra config>`
  made `git status` run `core.fsmonitor` and `git add` run a `filter.x.clean` command. Running the same
  calls with `GIT_DIR=<repo>/.git/worktrees/<name>` and `GIT_WORK_TREE=<worktree>` executed nothing. Note:
  in git config, `;` starts a comment, so test payloads must not contain `;` unquoted. Use a command
  like `touch /abs/path/marker` (quote the value: `fsmonitor = "touch /abs/marker"`).
- In `asyncio`, `Process.returncode` is available once the leader is reaped, even while a grandchild
  holds the pipes open; `Process.wait()` also waits for the pipes.
- Keep 100% line and branch coverage of `gaia/`: every new branch needs a test.
- Commands: run everything from `backend/`, e.g. `uv run pytest -q`, `uv run pytest tests/test_git_service.py -q`,
  `uv run mypy`, `uv run ruff check .`, `uv run ruff format .`, `uv run pytest --cov -q`, and for the
  migration `uv run alembic upgrade head` then `uv run alembic revision --autogenerate -m "worktree git_dir and approval idempotency key" --rev-id 0002`
  (the sandbox points `GAIA_DATA_DIR` at a temp dir; a fresh DB each run is fine).

## 2. What you may and must not touch
**You may create or edit only:** the files listed under "Files" in `docs/implementation/05b-git-hardening.md`,
`backend/tests/**`, and the "Decisions and deviations (as built)" section of that doc.

**You must not:** edit other docs, `.github/`, `Makefile`, `.pre-commit-config.yaml`, the `[tool.*]`
sections of `backend/pyproject.toml`, `gaia/security/**`, or existing migrations; add dependencies;
add `# noqa` / `# type: ignore` / skipped tests without a reason comment; edit existing tests to make
them pass (extending them is fine, and changing a test whose *behaviour under test* this package
deliberately changes is fine — say so in the report); build anything listed as out of scope.

## 3. Non-negotiable rules
- Secrets: none in code, tests or the report. Tests use fake values. Never print environment variables.
- Processes: only `gaia/services/proc.py` spawns processes in `gaia/` (tests may use `subprocess`, as the existing ones do).
- SQL: SQLAlchemy only. Tenant tables via `ScopedRepo`.
- No real network calls in tests.
- Library APIs: confirm against the installed source under `backend/.venv/lib/python3.12/site-packages/`.

## 4. How to work
1. Plan first: files, new public functions/classes with signatures, and how each acceptance criterion is proven.
2. Implement one task at a time, with its tests, and run them.
3. Before finishing, all must pass: `uv run ruff check .`, `uv run ruff format --check .`, `uv run mypy`,
   `uv run pytest --cov -q` (100% coverage).
4. Fill in "Decisions and deviations (as built)" in the doc.

## 5. When blocked
If the doc is ambiguous, contradicts the code, or you need a file outside your allowed paths or a new
dependency, stop and put the question under **Open questions** in your report, with what you have so far.

## 6. Report (the `finish` tool's `report` argument), in markdown
## Summary
## Plan vs. result
## Acceptance criteria   (each criterion, checked or not, with evidence: test names / command output)
## Deviations from the doc
## External APIs used   (library@version, API, source file checked)
## Security-relevant changes
## Tests added
## TODOs left for other packages
## Open questions
## Final check output   (the last lines of ruff, mypy and pytest --cov)
