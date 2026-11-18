# Gaia developer commands. Run `make` (or `make help`) to list them.
# Every target is safe to run on a clean clone after `make setup`.

BACKEND := backend
FRONTEND := frontend

.DEFAULT_GOAL := help
.PHONY: help setup setup-worktree migrate dev-backend dev-frontend run ui-url demo fmt lint typecheck test secrets smoke-llm openapi graph sandbox-install check audit

# `make run` serves the built UI from the API itself, so the login link uses the API's origin.
SERVE_ORIGIN = http://127.0.0.1:$${GAIA_PORT:-8765}
UI_DIST := $(abspath $(FRONTEND))/dist

help: ## List the available targets
	@grep -E '^[a-zA-Z_-]+:.*## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*## "}; {printf "  %-14s %s\n", $$1, $$2}'

setup: ## Install backend and frontend dependencies and the git hooks
	cd $(BACKEND) && uv sync --locked
	cd $(FRONTEND) && npm ci
	cd $(FRONTEND)/tools/openapi && npm ci
	cd $(BACKEND) && uv run pre-commit install

setup-worktree: ## Install dependencies only (no git hooks): the setup command to register Gaia itself with
	cd $(BACKEND) && uv sync --locked
	cd $(FRONTEND) && npm ci
	cd $(FRONTEND)/tools/openapi && npm ci

sandbox-install: ## Install the pinned sandbox-runtime (srt) binary under GAIA_DATA_DIR
	npm install --prefix "$${GAIA_DATA_DIR:-$$HOME/.gaia}/tools" --save-exact --ignore-scripts @anthropic-ai/sandbox-runtime@0.0.81

migrate: ## Create or upgrade the database schema in ~/.gaia/gaia.sqlite
	cd $(BACKEND) && uv run alembic upgrade head

dev-backend: ## Run the API server on the host and port from Settings
	cd $(BACKEND) && uv run gaia serve --reload

run: migrate ## Build the UI and serve Gaia (API + UI) on 127.0.0.1:8765; prints your login link
	cd $(FRONTEND) && npm run build
	cd $(BACKEND) && GAIA_UI_ORIGIN=$(SERVE_ORIGIN) uv run gaia ui
	cd $(BACKEND) && GAIA_UI_DIST_DIR=$(UI_DIST) GAIA_UI_ORIGIN=$(SERVE_ORIGIN) uv run gaia serve

ui-url: ## Print the login link for the UI that `make run` serves
	cd $(BACKEND) && GAIA_UI_ORIGIN=$(SERVE_ORIGIN) uv run gaia ui

demo: migrate ## Create the demo repo and ticket, then run Gaia with fake models (no keys, no cost)
	cd $(BACKEND) && uv run gaia demo init
	GAIA_FAKE_LLM=1 $(MAKE) run

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

smoke-llm: ## Make one tiny real call to each LLM provider (uses keychain keys)
	cd $(BACKEND) && uv run python scripts/smoke_llm.py

openapi: ## Export the API schema and regenerate the frontend's types (docs 06, 16)
	cd $(BACKEND) && uv run python scripts/export_openapi.py
	cd $(FRONTEND) && npm run gen:api

graph: ## Export the LangGraph Mermaid diagram to docs/graph.md
	cd $(BACKEND) && uv run python scripts/export_graph.py

secrets: ## Scan the full git history for secrets with gitleaks
	@command -v gitleaks >/dev/null || { echo "gitleaks not found: brew install gitleaks" >&2; exit 1; }
	gitleaks git --redact --no-banner .

audit: ## Scan locked backend and frontend dependencies for known vulnerabilities (doc 18c)
	cd $(BACKEND) && uv export --frozen --no-emit-project --format requirements-txt \
		| uvx --from pip-audit==2.10.1 pip-audit --strict --disable-pip -r /dev/stdin
	cd $(FRONTEND) && node tools/audit/audit-check.mjs
	cd $(FRONTEND)/tools/openapi && node ../audit/audit-check.mjs

check: lint typecheck test secrets ## Everything CI runs: lint, typecheck, test, secrets
