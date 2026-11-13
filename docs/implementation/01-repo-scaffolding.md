# 01 — Repo Scaffolding & Tooling

## Goal
An empty but fully wired monorepo. Lint, typecheck, test and secret scanning work
locally and in CI.

## Out of scope
Any application logic.

## Tasks
1. Create `backend/` with `uv init --package gaia` (Python 3.12). Dependencies:
   - runtime: `fastapi`, `uvicorn[standard]`, `pydantic>=2`, `pydantic-settings`, `sqlalchemy>=2`,
     `aiosqlite`, `alembic`, `structlog`, `keyring`, `httpx`
   - dev: `pytest`, `pytest-asyncio`, `pytest-cov`, `ruff`, `mypy`, `pre-commit`
   (LangGraph, the Anthropic SDK and others are added by later docs.)
2. Package skeleton: `backend/gaia/{config,db,domain,services,llm,runtimes,tools,orchestrator,api,security}/__init__.py`,
   `backend/tests/`.
3. `backend/pyproject.toml`: configure ruff (`select = ["E","F","I","B","S","UP","ASYNC"]` —
   `S` is bandit security), mypy (`strict = true` for `gaia.*`), and pytest (`asyncio_mode = "auto"`).
4. Create `frontend/` with `npm create vite@latest frontend -- --template react-ts`. Add eslint,
   prettier and vitest. Pin exact versions (no `^`).
5. Root `Makefile` targets: `setup`, `dev-backend`, `dev-frontend`, `lint`, `typecheck`, `test`,
   `check` (all of the above plus gitleaks).
6. `.pre-commit-config.yaml`: ruff, ruff-format, mypy, `gitleaks` (official hook), end-of-file-fixer,
   check-added-large-files.
7. `.gitignore`: `.venv`, `node_modules`, `*.sqlite*`, `.env*`, `dist`, `.gaia/` (local data dir),
   `worktrees/`.
8. `.github/workflows/ci.yml`: run `make check` on pull requests. Pin actions by commit SHA.
   Set `permissions: contents: read`.
9. Add a `README.md` quickstart: prerequisites (uv, node 20+, git, gh), then `make setup`, then `make dev-*`.
10. A trivial test in each of backend and frontend so CI is green.

## Acceptance criteria
- [ ] `make setup && make check` passes on a clean clone.
- [ ] Committing a file containing a fake key such as `sk-ant-api03-AAAA...` is blocked by pre-commit.
- [ ] CI runs on a PR and passes.
