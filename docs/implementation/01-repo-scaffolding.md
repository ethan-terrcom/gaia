# 01 — Repo Scaffolding & Tooling

**Status:** done (PR `impl/01-repo-scaffolding`). This doc now records what was built, so
later docs can rely on these conventions.

## Goal
An empty but fully wired monorepo. Lint, typecheck, test and secret scanning work
locally and in CI.

## Out of scope
Any application logic.

## Tasks (as built)
1. `backend/` is a uv project with a **flat layout** (`backend/gaia/`, as ARCHITECTURE §2 shows),
   not uv's default `src/` layout: `uv_build` with `module-root = ""`. Python is pinned to
   3.12 (`backend/.python-version`, `requires-python = "==3.12.*"`). Dependencies:
   - runtime: `fastapi`, `uvicorn[standard]`, `pydantic`, `pydantic-settings`, `sqlalchemy` (2.1),
     `aiosqlite`, `alembic`, `structlog`, `keyring`, `httpx`
   - dev: `pytest`, `pytest-asyncio`, `pytest-cov`, `ruff`, `mypy`, `pre-commit`
   (LangGraph, the Anthropic SDK and others are added by later docs, with `uv add`.)
2. Package skeleton: `backend/gaia/{config,db,domain,services,llm,runtimes,tools,orchestrator,api,security}/__init__.py`.
   Each `__init__` docstring says what the package owns. A test fails if one is missing.
3. `backend/pyproject.toml`:
   - ruff: `E, F, I, B, S, UP, ASYNC` as planned, plus `RUF`, `PT` and `TID`. **`TID251` bans
     `subprocess`, `os.system`, `os.popen`, `asyncio.create_subprocess_*` and `pickle` everywhere
     except `gaia/services/proc.py`.** This turns doc 05's "only proc.py spawns processes"
     and doc 18d's "no `shell=True`" into lint errors rather than review items.
   - mypy: `strict = true` for `gaia` and `tests` (tests may omit annotations), with the pydantic plugin.
   - pytest: `asyncio_mode = "auto"`, `--strict-markers`, `filterwarnings = ["error"]` (catches
     un-awaited coroutines), and a `live` marker that is **deselected by default** (`-m live` to opt in).
4. `frontend/` comes from `npm create vite@latest -- --template react-ts` (create-vite 9: Vite 8,
   React 19, TypeScript 7). **The template now ships oxlint, not ESLint**, so we keep oxlint and add
   prettier and vitest (jsdom + Testing Library). Exact versions only (`save-exact=true`), and
   **`ignore-scripts=true`** in `frontend/.npmrc` (no dependency install scripts). TypeScript runs with
   `strict` and `noUncheckedIndexedAccess`. The dev server binds to `127.0.0.1:5173` with `strictPort`.
5. Root `Makefile`: `help` (default), `setup`, `dev-backend` (placeholder until doc 04), `dev-frontend`,
   `fmt`, `lint`, `typecheck`, `test`, `secrets`, and `check` = lint + typecheck + test + secrets.
6. `.pre-commit-config.yaml`: the official `gitleaks` hook (pre-commit bootstraps Go itself) and
   `pre-commit-hooks` (end-of-file, trailing whitespace, large files, merge conflicts, private keys,
   YAML/TOML), both pinned by SHA. The ruff, mypy and frontend-lint hooks are `local` hooks that run
   the project's own pinned tools, so they never drift from CI.
7. `.gitignore`: `.venv`, `node_modules`, `*.sqlite*`, `.env*`, `dist`, `.gaia/`, `worktrees/`, caches.
   `.gitleaks.toml` extends the default rules with a DeepSeek key rule (`sk-` + 32 hex).
8. `.github/workflows/ci.yml`: `make setup && make check` on pull requests and pushes to `main`.
   Actions are pinned by SHA, `permissions: contents: read`, `persist-credentials: false`, full-history
   checkout for gitleaks, and the gitleaks binary is downloaded and **checksum-verified**.
9. `README.md` quickstart: prerequisites (uv, Node 26 LTS, git, gh, gitleaks), `make setup`,
   `make check`, `make dev-*`.
10. A trivial test in each of backend and frontend.

## Acceptance criteria
- [x] `make setup && make check` passes on a clean clone.
- [x] Committing a file containing a fake Anthropic key (`sk-ant-api03-...`) or DeepSeek key is blocked by pre-commit.
- [ ] CI runs on a PR and passes. (Checked on the PR itself.)

## For later docs
- Add dependencies with `uv add <pkg>` (backend) or `npm install -D <pkg>` (frontend). Never hand-edit
  `uv.lock` or `package-lock.json`.
- New process-spawning code goes in `gaia/services/proc.py` only (doc 05). The lint rule will stop you elsewhere.
- Tests that need the network or real keys get `@pytest.mark.live` and never run in CI.
