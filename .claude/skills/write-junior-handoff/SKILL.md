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
- **Name parallel work.** If another package is being built at the same time, say what to stub and
  which `TODO(gaia-NN)` to leave. An example is 07b writing an event row because 13's bus did not
  exist yet.

## Sizing and parallel dispatch
- One handoff is one PR. About 15–20M prompt tokens (about 99% cache hits) and 80–100K output
  tokens is normal. More than 220 turns means the package is too big: split it.
- Packages dispatched together must not both add a migration with the same revision. Give each a
  distinct `--rev-id` in order, and expect to rebase the later one onto `0004 → 0005`.
- Expect conflicts in `api/app.py`, `api/deps.py` (`AppServices`) and `db/repo.py`. They are small,
  and the lead resolves them at merge.

## Checklist before dispatch
- [ ] The worktree is cut from the current `main`, and `uv sync` plus `npm ci` have run there. The
      sandbox has no network.
- [ ] Every new dependency is added by the lead before dispatch, pinned to a version at least two
      weeks old.
- [ ] The handoff is copied into the worktree, and the `run` allowlist covers every command it
      names.
- [ ] Each acceptance criterion maps to either a named test or a named lead step.

The full process, with commands, lives in `docs/operations/running-junior-agents.md`.
