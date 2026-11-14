---
name: review-junior-package
description: Review a work package that a DeepSeek junior implemented for Gaia. It covers the bug classes juniors' tests hide, what to fix yourself versus flag to the owner, live verification, and the review doc. Use after a junior run finishes, or when Gaia's lead reviews a junior's diff.
---

# Reviewing a junior's package

Juniors reliably produce code that passes their own tests. They miss bugs where the tests' **model
of the world** is wrong. Review against the real world, not against their tests.

## 1. Mechanical pass (5 minutes)
- Run `make check` in the worktree: ruff, format, mypy, pytest with 100% coverage, gitleaks.
- Run `git status` and `git diff --stat main`. Every touched file must be on the handoff's "may
  touch" list. Watch for untracked junk such as `*.tmp`, `.coverage` and scratch scripts.
- Search for any new `# type: ignore`, `# noqa`, `pragma: no cover` or `pytest.skip`. Each needs a
  reason that holds up.
- Read the report's "Deviations" and "Open questions" before reading the code.

## 2. The bug classes juniors' tests hide (check each one)
| Class | What happened in Gaia | How to check |
|---|---|---|
| **Stubbed API shape** | 07a sent `tool_choice` without `tools`. A stub accepted it; the live API returns 400. | For every external call, compare the request against the provider docs and the installed SDK source, not against the test double. |
| **Destructive cascades** | 06's "unregister repository" would have cascade-deleted tickets and run history. | Grep for `delete`, `ondelete=` and `cascade`. Ask what disappears with it. |
| **Skipped safety checks on the "force" path** | 05b's `remove(force=True)` skipped the tamper check. | Follow every boolean flag's branch, and the cancellation and error paths. |
| **Leaky strings** | Remote URLs kept `user:token@`. Error messages carried `str(exc)`. | Grep for responses, logs and exceptions that interpolate input or upstream text. |
| **Unbounded waits** | A cancelled process could hang on an escaped grandchild. | Every await on I/O or a process needs a timeout or a bounded reap. |
| **Misread state** | Orphan worktrees showed as "tampered". | Check what the code concludes when a precondition (exists, is in git) is false. |
| **Async lifetime** | Background tasks and stream generators. | Is it cancelled and awaited on shutdown? Is the session opened inside the generator? |
| **Generated artifacts** | `openapi.json` failed the frontend lint. | Run the repo-wide hooks, not only the backend checks. |
| **Cancellation in `finally`** | 07b: Starlette cancels a streaming body through an anyio cancel scope, which also cancelled the usage recording in `finally`, so a client disconnect made the call free. | Any `await` in `finally`/cleanup of a streaming or cancellable path needs `anyio.CancelScope(shield=True)` plus a timeout. **Write a reproducing test first** (an ASGI-level `http.disconnect`). |
| **Missing data defaults to free or zero** | 07b: a stream without a usage block was recorded at $0. | Ask "what happens when the field never arrives?" Charge a conservative estimate, never zero. |
| **Unbounded growth** | 13 and 07b: every ticket or token was added to a redaction registry that is never pruned and is scanned on every log line. 13: a new SSE connection replayed the whole event log into memory. | Look for module-level sets and dicts, `limit=None`, and "replay from 0". Prefer patterns, TTLs and paging. |
| **Precedence bugs in policy code** | 09a: the owner's `extra_allow` was checked after the tripwires, so it could never take effect. A symlink to `.git` passed a check made on the input string only. | For every allow or deny rule, write the case that should win and the case that should lose. Check the *resolved* value, not the spelling. |
| **Conversation shape** | 11: a "revise your plan" retry was sent without the plan being revised. | When code builds a multi-turn request, print the message roles and contents in a test. |
| **Mocks that loop** | 13: a mocked `replay` published new events on every call, which was harmless until the code under test started calling it in a loop. | When fixing behaviour, re-read the mocks the junior wrote; they encode the old behaviour. |
| **Coverage that depends on the environment** | 10a: a `ps`-failure branch was covered only because `ps` could not start inside the junior's sandbox; outside it, coverage was 99%. | Rerun `--cov` outside the harness. Every error branch needs a deterministic test (monkeypatch the call). |
| **macOS hides Linux process behaviour** | 10a: a "pid is gone" check passed on macOS but failed in Linux CI, because a killed child stays a zombie until asyncio reaps it. | Process tests must yield to the event loop and treat zombies (`/proc/<pid>/stat` state `Z`) as gone. CI on Linux is the arbiter. |
| **Silent framework fallbacks** | 12a: LangGraph silently drops a route to an unknown node. Conditional edges without path maps let a typo stop a run, and the diagram showed 4 of 28 edges. | Look at generated artefacts (diagrams, schemas): a thin one is a symptom. Declare every branch target. |
| **Binding a global at the wrong time** | 11: `configure_logging(stream=sys.stderr)` pinned pytest's capture stream, which is closed after the test. | Pass a proxy that resolves the global at use time. |

## 3. Fix or flag
- **Fix it yourself**, in the same branch, with a test: small bugs, missing edge cases, wrong
  status codes, docs, naming, and the API-shape bugs above when the fix is local. Mention each fix
  in the review doc and the commit message as a "lead fix".
- **Flag it to the owner** with a recommendation, and do not silently change it: product scope,
  cost, security trade-offs, anything that removes data, and anything that changes a published
  contract. Use `docs/reviews/` plus a question in the final report.

## 4. Live verification
The junior cannot reach the network. Run the acceptance criterion yourself:
- Use a temporary `GAIA_DATA_DIR`, run `alembic upgrade head` and `gaia serve` on a spare port
  (8799), and keep the token in a shell variable. Never echo it.
- Check after a live run that the token or key appears **zero times** in the server log
  (`grep -c`).
- Record the exact commands and their outcomes in the review doc.

## 5. Document it (the owner reads this first)
- `docs/reviews/<date>-review-<NN>.md` has these sections: summary verdict; a findings table with
  ID, severity, status (fixed or flagged), location and recommendation; lead fixes; live
  verification; and the cost of the run.
- Add a "Decisions and deviations (as built)" section to the package doc, if the junior didn't.
- Update the developer guides in `docs/backend/` (explainers for intermediate Python devs). Update
  `backend/README.md`: the "What exists today" row, the diagram, and the test count.
- Update the memory and the findings tracker if any finding stays open.

## 6. Land it
Follow `land-junior-package`.

The full process, with commands, lives in `docs/operations/running-junior-agents.md`.
