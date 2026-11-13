# Implementation Plan — Index

These docs are work packages for implementing engineers and agents. Each row below is **one PR**.
Some docs are split into lettered sub-PRs (07a/07b, 09a/09b, …); the split is described at the
top of each doc. A package may start only when everything in its "Depends on" column is merged.

Before you start any package, read `../PROJECT_BRIEF.md` and `../ARCHITECTURE.md`. Junior agents
receive their assignment through [`HANDOFF_TEMPLATE.md`](HANDOFF_TEMPLATE.md).

The 2026-11-12 plan review ([`../reviews/2026-11-12-plan-review.md`](../reviews/2026-11-12-plan-review.md))
revised this table. Rows marked **(D*n*)** came from owner decisions in that review; all four were
accepted as recommended on 2026-11-13.

| # | Package | Depends on | Can run alongside | Owner |
|---|---|---|---|---|
| 01 | Repo scaffolding & tooling | – | – | lead · done (PR #2) |
| 02 | Config & secrets | 01 | 16 | junior |
| 03 | Database & domain models | 02 | 16 | junior |
| 04 | API skeleton & local auth | 03 | 05, 07a, 12a | junior |
| 05 | Git & worktree service | 03 | 04, 07a, 12a | junior |
| 06 | Repositories & tickets API | 04, 05 | 07b, 08, 09a, 10a, 11, 13 | junior |
| 07a | LLM clients & cost accounting | 03 | 04, 05, 12a | junior |
| 07b | LLM gateway **(D2)** | 04, 07a | 06, 08, 09a, 10a, 11, 13 | junior · lead review |
| 08 | Runtime spike → ADR-001 (decision gate) | 05, 07a | 06, 07b, 09a, 10a, 11, 13 | **lead** · owner signs ADR |
| 09a | Path guard, command policy, sandbox profile **(D1)** | 05 | 06, 07b, 08, 10a, 11, 13 | junior · **architect review** |
| 09b | Sandboxed tools, registry, audit | 09a | 10b, 11, 15a | junior · architect review |
| 10a | Runtime base types, contract suite, fake runtime | 05 | 06–09, 11, 13 | junior |
| 10b… | One PR per runtime chosen in ADR-001 | 08 (signed), 09b, 10a, 07b | each other, 15a | junior |
| 11 | Lead planning & review | 07a | 06–10, 13 | junior code · **lead writes prompts** |
| 12a | Orchestrator skeleton on fakes | 03 | 04–11, 13 | **lead** (or pair-reviewed) |
| 12b | Real nodes wired to services | 10b, 11, 12a | 13, 15a | junior |
| 13 | Event bus & SSE | 04 | 05–12 | junior |
| 14 | Runs & approvals API | 12b, 13 | 15b, 16 | junior |
| 15a | GitHub: push, open PR, merge | 05, 06 | 09b, 10b, 12b, 13 | junior |
| 15b | GitHub: CI watch, PR sync, PR API | 15a | 14, 16 | junior |
| 15c | GitHub: import issues (optional in v1) | 06, 15a | anything | junior |
| 16 | Frontend shell | 01 (stub schema); 04 and 06 to finish | 02–15 | junior |
| 17a–f | Frontend views (one PR each) | 13, 14, 15b, 16 | each other, 18 | junior |
| 18a–d | Budgets, observability, hardening, security review | 12b | 17 | junior · 18d architect |

### Waves (the critical path, with maximum parallelism)
1. **02**, **16** (shell against a stub schema)
2. **03**
3. **04**, **05**, **07a**, **12a** (the riskiest component starts early, on fakes)
4. **06**, **07b**, **08**, **09a**, **10a**, **11**, **13**
5. After ADR-001 is signed: **09b**, **10b…**, **15a**
6. **12b**, **15b**
7. **14**, **15c**
8. **17a–f**, **18a–d**

## Rules for every work package

1. **Stay in scope.** If a doc's "Out of scope" lists something, do not build it. Leave a
   `# TODO(gaia-NN):` comment that points to the doc that owns it.
2. **Branch:** `impl/NN-short-name` (for example `impl/07a-llm-clients`). One PR per package. Never merge
   your own PR; the owner approves merges.
3. **Definition of done:** every acceptance criterion is checked with evidence, and `make check` passes
   (lint, typecheck, tests, gitleaks). New code has tests, including failure paths. No new
   `# type: ignore` or `# noqa` without a comment saying why. If the implementation diverges from the
   doc, update the doc in the same PR.
4. **Security:** never commit, log or echo secrets; never use `shell=True`; never build SQL with string
   formatting; never trust a path without `security.paths.confine()` (from 09a; before 09a exists, don't
   accept user paths); never run git outside `GitService`'s hardened environment (doc 05).
5. **Workspace scoping:** every query on a tenant table goes through the scoped repository
   helpers from doc 03.
6. **When unsure,** stop and write the question in the PR description. Do not guess at
   architecture.
7. **Library APIs:** check against the official docs **for the installed version** (Anthropic SDK,
   Claude Agent SDK, LangGraph, FastAPI, SQLAlchemy). Do not write library calls from memory. List each
   external API you used, with its doc URL, in the PR description.
8. **Guardrails from doc 01 are not negotiable.** Ruff's banned-API rule (only `gaia/services/proc.py`
   spawns processes), strict mypy and strict TypeScript, warnings-as-errors in pytest, and exact
   dependency pins. Do not relax them to make a check pass; ask instead.
