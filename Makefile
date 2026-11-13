# Gaia developer commands. Run `make` (or `make help`) to list them.
# Every target is safe to run on a clean clone after `make setup`.

BACKEND := backend
FRONTEND := frontend

.DEFAULT_GOAL := help
.PHONY: help setup migrate dev-backend dev-frontend fmt lint typecheck test secrets check

help: ## List the available targets
	@grep -E '^[a-zA-Z_-]+:.*## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*## "}; {printf "  %-14s %s\n", $$1, $$2}'

setup: ## Install backend and frontend dependencies and the git hooks
	cd $(BACKEND) && uv sync --locked
	cd $(FRONTEND) && npm ci
	cd $(BACKEND) && uv run pre-commit install

migrate: ## Create or upgrade the database schema in ~/.gaia/gaia.sqlite
	cd $(BACKEND) && uv run alembic upgrade head

dev-backend: ## Run the API server (implemented by doc 04)
	@# TODO(gaia-04): run uvicorn with the app factory, host and port taken from Settings.
	@echo "dev-backend is implemented by doc 04 (API skeleton & local auth)." >&2; exit 1

dev-frontend: ## Run the Vite dev server on 127.0.0.1:5173
	cd $(FRONTEND) && npm run dev

fmt: ## Auto-format and auto-fix backend and frontend
	cd $(BACKEND) && uv run ruff format . && uv run ruff check --fix .
	cd $(FRONTEND) && npm run format

lint: ## Lint backend (ruff) and frontend (oxlint, prettier)
	cd $(BACKEND) && uv run ruff check . && uv run ruff format --check .
	cd $(FRONTEND) && npm run lint

typecheck: ## Type-check backend (mypy strict) and frontend (tsc)
	cd $(BACKEND) && uv run mypy
	cd $(FRONTEND) && npm run typecheck

test: ## Run backend (pytest) and frontend (vitest) tests
	cd $(BACKEND) && uv run pytest --cov
	cd $(FRONTEND) && npm test

secrets: ## Scan the full git history for secrets with gitleaks
	@command -v gitleaks >/dev/null || { echo "gitleaks not found: brew install gitleaks" >&2; exit 1; }
	gitleaks git --redact --no-banner .

check: lint typecheck test secrets ## Everything CI runs: lint, typecheck, test, secrets
