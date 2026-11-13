# Implementation Plan — Index

These docs are work packages for implementing engineers and agents. Do them **in
order**, unless the dependencies column says a doc can run in parallel. Each doc is
sized to be one PR.

Before you start any doc, read `../PROJECT_BRIEF.md` and `../ARCHITECTURE.md`.

| # | Doc | Depends on | Parallel-safe with |
|---|---|---|---|
| 01 | Repo scaffolding & tooling | – | – |
| 02 | Config & secrets | 01 | 16 |
| 03 | Database & domain models | 02 | 16 |
| 04 | API skeleton & local auth | 03 | 05, 07 |
| 05 | Git & worktree service | 03 | 04, 07 |
| 06 | Repositories & tickets API | 04, 05 | 07 |
| 07 | LLM providers & cost accounting | 02 | 04, 05, 06 |
| 08 | **Runtime spike (decision gate)** | 05, 07 | 09 |
| 09 | Sandboxed tools & command policy | 05 | 08 |
| 10 | Agent runtimes | 08, 09 | – |
| 11 | Lead planning & review | 07 | 10 |
| 12 | Orchestrator graph (LangGraph) | 10, 11 | – |
| 13 | Event bus & SSE | 04 | 07–11 |
| 14 | Approvals API | 12, 13 | 15 |
| 15 | GitHub integration | 05, 06 | 12–14 |
| 16 | Frontend shell | 01 | 02–15 |
| 17 | Frontend views | 13, 14, 15, 16 | – |
| 18 | Budgets, observability & hardening | 12 | 17 |

## Rules for every work package

1. **Stay in scope.** If a doc's "Out of scope" lists something, do not build it. Leave a
   `# TODO(gaia-NN):` comment that points to the doc that owns it.
2. **Branch:** `impl/NN-short-name`. One PR per doc.
3. **Definition of done:** every acceptance criterion is checked, and `make check` passes
   (lint, typecheck, tests, gitleaks). New code has tests. No new `# type: ignore` without
   a comment saying why.
4. **Security:** never commit secrets, never log secrets, never use `shell=True`, never
   build SQL with string formatting, never trust a path without `security.paths.confine()`
   (from doc 09; before 09 exists, don't accept user paths).
5. **Workspace scoping:** every query on a tenant table goes through the scoped repository
   helpers from doc 03.
6. **When unsure,** stop and write the question in the PR description. Do not guess at
   architecture.
7. **Library APIs:** check against the official docs for the installed version (Anthropic
   SDK, LangGraph, FastAPI). Do not write library calls from memory.
