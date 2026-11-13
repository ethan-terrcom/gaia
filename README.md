# Gaia

A local-first control plane for a hierarchy of AI coding agents: a lead engineer agent
(Claude) that plans and reviews, junior agents (DeepSeek) that implement in isolated git
worktrees, and a human approval inbox for every irreversible step.

- What and why: [docs/PROJECT_BRIEF.md](docs/PROJECT_BRIEF.md)
- How: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)
- Work packages: [docs/implementation/00-README.md](docs/implementation/00-README.md)

## Prerequisites

| Tool | Version | Install (macOS) |
|---|---|---|
| [uv](https://docs.astral.sh/uv/) | 0.13+ | `brew install uv` (it installs Python 3.12 for you) |
| Node.js | 26 LTS (24 also works; see `.nvmrc`) | `brew install node` |
| git | 2.40+ | Xcode command line tools |
| [gh](https://cli.github.com/) | 2.x | `brew install gh` |
| [gitleaks](https://github.com/gitleaks/gitleaks) | 8.30+ | `brew install gitleaks` |

## Quickstart

```sh
make setup         # backend + frontend dependencies, and the pre-commit hooks
make check         # lint, typecheck, test and secret scan (exactly what CI runs)
make dev-frontend  # UI on http://127.0.0.1:5173
make dev-backend   # API server (arrives with doc 04)
```

Run `make` with no arguments to list every target.

## Repository layout

```
backend/    Python 3.12 package `gaia` (FastAPI, LangGraph), managed with uv
frontend/   Vite + React + TypeScript UI
docs/       brief, architecture, decisions and implementation work packages
```

## Conventions that tooling enforces

- **Secrets never touch the repo or env files.** API keys live in the macOS Keychain
  (doc 02). gitleaks runs in pre-commit and CI.
- **Only `gaia/services/proc.py` may spawn processes**, and never through a shell. Ruff
  rejects `subprocess`, `os.system` and similar imports everywhere else.
- **Strict typing.** mypy runs in strict mode on the backend. TypeScript runs in strict mode on the frontend.
- **Warnings are errors in tests**, so un-awaited coroutines fail the build.
- **Exact dependency versions.** `uv.lock` pins the backend, and `package.json` holds exact
  versions with no `^`. npm install scripts are disabled (`frontend/.npmrc`).
