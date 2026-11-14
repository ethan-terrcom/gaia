# Gaia: notes for Claude sessions

Gaia is a local-first control plane for AI coding agents. Start with `docs/PROJECT_BRIEF.md`,
`docs/ARCHITECTURE.md` and `backend/README.md`.

- **Building packages:** a lead session writes handoffs, runs DeepSeek juniors in a sandbox,
  reviews their work and merges it. See
  [`docs/operations/running-junior-agents.md`](docs/operations/running-junior-agents.md) and the
  skills in `.claude/skills/`.
- **Branches:** one branch per package, cut from the current `main`; PRs target `main`. Never stack
  PRs, and never commit on the main checkout. Merging needs the owner's permission for the session,
  plus green CI.
- **Checks:** `make check` (ruff, mypy, pytest at 100% coverage, gitleaks, frontend lint) must pass.
  Never relax a guardrail in `pyproject.toml` to make a check pass; ask instead.
- **Secrets:** never print, log or commit keys. Keys come from the macOS Keychain (`gaia secrets`),
  or for the lead's tooling from `~/.zshrc` via `zsh -ic`. Check that a key is set without echoing
  it.
- **Documentation is part of done:** a review in `docs/reviews/`, a "Decisions and deviations (as
  built)" section in the package doc, updated `docs/backend/` guides, and an updated
  `backend/README.md`.
