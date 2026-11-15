---
name: write-junior-handoff
description: Write the handoff that a DeepSeek junior agent implements a Gaia work package from. Use when assigning an implementation doc (docs/implementation/NN-*.md) to a junior, or when Gaia's lead plans steps for a junior runtime.
---

# Writing a handoff for a DeepSeek junior

A handoff turns one implementation doc into instructions a capable but literal junior can finish
alone, offline, in one session. Juniors work well when the design is already decided. They work
badly when they have to discover a library's real behaviour, because they have no network access.

Start from `docs/implementation/HANDOFF_TEMPLATE.md`. Save the result as
`docs/implementation/handoffs/<NN>-<name>.md` **on the package branch**, never on `main`. The
existing handoffs in that folder are worked examples.

## Structure (keep this order)
1. **Header:** the harness's limits, which are no network, no git writes and no installs. State that
   the lead commits and merges, and that the report goes through `finish`.
2. **Read first:** the doc, the 00-README rules, the relevant ARCHITECTURE sections, the backend
   guides, the latest reviews of the packages it builds on, and the exact code files.
3. **Lead notes:** the design decisions, already made. This is where the value is (see below).
4. **May and must not touch:** an explicit file list. Name the `[tool.*]` sections, `uv.lock`,
   `.github/`, other docs and existing migrations as forbidden, unless the package needs them.
5. **Non-negotiable rules:** secrets, SQL only in `gaia/db/`, fail closed, and checking library APIs
   against the installed source in `backend/.venv/lib/python3.12/site-packages/`.
6. **How to work:** plan first, the build order, the required tests by name, and the full check
   command list.
7. **When blocked:** write the question under "Open questions". Do not guess.
8. **Report format:** fixed headings, including "Deviations", "External APIs used",
   "Security-relevant changes" and "Final check output".

## What goes in the lead notes
- **Decide the design.** Give the class and function signatures, table and column names, the
  migration `--rev-id`, error-to-status mappings, and which existing helper to reuse.
  Under-specified security choices come back as the most permissive reading.
- **Give facts the junior cannot look up.** Version-pinned library behaviour, a third-party tool's
  config schema, and an API's real constraints. Read the real docs or package yourself first, for
  example `npm pack <pkg>@<version>` into an isolated scratch folder.
- **Give the testability seams.** Juniors get stuck when something is hard to test. Tell them:
  - `create_app(..., llm_transport=httpx.MockTransport)` injects upstreams;
  - `httpx.ASGITransport` buffers whole responses, so write SSE as a standalone async generator;
  - coverage does not trace SQLAlchemy greenlets, so add a direct call test when a line shows missed;
  - clocks are injectable (`now: Callable[[], float]`), so tests never sleep.
- **Say what they cannot prove, and give the lead a hook.** A live `curl`, a real sandbox or a real
  provider call is out of their reach. Ask for a test-level proof plus a dev-only entry point the
  lead runs: a `scripts/dev_*.py` file, or a route that exists only when `GAIA_DEV_ROUTES=true`.
- **Pre-decide security tightenings** and ask them to record each as a deviation. An example is
  09a, where `git` is denied in `run_command` in favour of GitService tools.
- **Place the boundary's own files.** Say where every file that configures a sandbox, a policy or
  a credential helper lives, and make sure the bounded process cannot write there (09b).
- **Say how a flow leaves a non-default state.** "Open as draft" needs "mark ready before merge"
  (15a). A handoff that chooses a state must name the step that undoes it.
- **Say where the transaction ends.** For anything that calls the network, write "load → network →
  persist", and say what is committed first when a later step can fail (15a).
- **Name the platform semantics a fake must copy.** A browser delivers named SSE events only to
  `addEventListener`. An HTTP path must be matched as sent. Put the platform rule in the lead
  notes, and ask for the fake to follow it (16, 15a).
- **Name parallel work.** If another package is being built at the same time, say what to stub and
  which `TODO(gaia-NN)` to leave. An example is 07b writing an event row because 13's bus did not
  exist yet.

## Sizing and parallel dispatch
- One handoff is one PR. About 15–45M prompt tokens (about 99% cache hits) and 80–150K output
  tokens is normal. 09b (10 tools, 4 cross-module edits) took 209 turns: give packages that size
  `--max-turns 260`, and split anything larger.
- **Signs a package is too big:** more than about 12 new modules, or more than 8 required
  integration tests. 12a (graph, 15 nodes, guard, runner, 8 graph tests) hit 240 turns
  unfinished. When that happens, measure what exists (`pytest`, `--cov`), then dispatch a
  **part 2** handoff that lists the exact failing tests, missed lines and missing files, with
  "finish it, don't redesign it". The 12a part-2 session finished in 79 turns.
- Parallel runs make each run slower (two-hour wall times with four at once); tests that spawn
  processes slow down most.
- Packages dispatched together must not both add a migration with the same revision. Give each a
  distinct `--rev-id` in order, and expect to rebase the later one onto `0004 → 0005`.
- Expect conflicts in `api/app.py`, `api/deps.py` (`AppServices`) and `db/repo.py`. They are small,
  and the lead resolves them at merge.

## Checklist before dispatch
- [ ] The worktree is cut from the current `main`, and `uv sync` plus `npm ci` have run there. The
      sandbox has no network.
- [ ] Every new dependency is added by the lead before dispatch, pinned to a version at least two
      weeks old. **Check the transitive ones too:** diff `uv.lock` and look up each new package's
      upload date. Hold back any that are too new with `[tool.uv] exclude-newer-package = { pkg =
      "<date>" }` (12a needed 9 of these for langgraph; 10c needed `sse-starlette`). A global
      `--exclude-newer` also downgrades your existing pins. Commit the setup as its own commit
      ("NN: lead setup"), so the junior's diff stays reviewable on its own.
- [ ] **npm:** check every new `package-lock.json` entry's publish date (`npm view <pkg> time`), and
      hold back young transitive packages with `"overrides"` in `package.json` (16 needed 3). If a
      tool's peer dependency conflicts with the app (`openapi-typescript` needs TypeScript 5; the app
      is on 7), give the tool its own pinned folder (`frontend/tools/<tool>/`). npm overrides do
      not apply to peers.
- [ ] Frontend packages: the harness allows `npm run {lint,format,typecheck,test,build,gen:api}`
      with `cwd: "frontend"`, offline. Install everything first.
- [ ] The handoff is copied into the worktree, and the `run` allowlist covers every command it
      names.
- [ ] Each acceptance criterion maps to either a named test or a named lead step.

The full process, with commands, lives in `docs/operations/running-junior-agents.md`.
