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
| **Cancellation in `finally`** | 07b: Starlette cancels a streaming body through an anyio cancel scope, which also cancelled the usage recording in `finally`, so a client disconnect made the call free. 09b: the tool registry's audit write had the same bug, triggered by the Agent SDK's task group, not by Gaia's own code. | Any `await` in `finally`/cleanup of a streaming or cancellable path needs `anyio.CancelScope(shield=True)` plus a timeout. **Write a reproducing test first** (an ASGI-level `http.disconnect`). |
| **Missing data defaults to free or zero** | 07b: a stream without a usage block was recorded at $0. | Ask "what happens when the field never arrives?" Charge a conservative estimate, never zero. |
| **Unbounded growth** | 13 and 07b: every ticket or token was added to a redaction registry that is never pruned and is scanned on every log line. 13: a new SSE connection replayed the whole event log into memory. | Look for module-level sets and dicts, `limit=None`, and "replay from 0". Prefer patterns, TTLs and paging. |
| **Precedence bugs in policy code** | 09a: the owner's `extra_allow` was checked after the tripwires, so it could never take effect. A symlink to `.git` passed a check made on the input string only. | For every allow or deny rule, write the case that should win and the case that should lose. Check the *resolved* value, not the spelling. |
| **Conversation shape** | 11: a "revise your plan" retry was sent without the plan being revised. | When code builds a multi-turn request, print the message roles and contents in a test. |
| **Mocks that loop** | 13: a mocked `replay` published new events on every call, which was harmless until the code under test started calling it in a loop. | When fixing behaviour, re-read the mocks the junior wrote; they encode the old behaviour. |
| **Coverage that depends on the environment** | 10a: a `ps`-failure branch was covered only because `ps` could not start inside the junior's sandbox; outside it, coverage was 99%. | Rerun `--cov` outside the harness. Every error branch needs a deterministic test (monkeypatch the call). |
| **macOS hides Linux process behaviour** | 10a: a "pid is gone" check passed on macOS but failed in Linux CI, because a killed child stays a zombie until asyncio reaps it. | Process tests must yield to the event loop and treat zombies (`/proc/<pid>/stat` state `Z`) as gone. CI on Linux is the arbiter. |
| **Silent framework fallbacks** | 12a: LangGraph silently drops a route to an unknown node. Conditional edges without path maps let a typo stop a run, and the diagram showed 4 of 28 edges. | Look at generated artefacts (diagrams, schemas): a thin one is a symptom. Declare every branch target. |
| **Fakes that don't follow the platform** | 15a: the GitHub fake matched httpx's *decoded* path, so `gaia%2Fabc` passed and remote branches were never deleted. 16: the fake `EventSource` called `onmessage` for named events, which no browser does, so the UI would have had no live updates at all. | Compare every fake against the platform's documented behaviour, not against the code under test. Match on the wire (`url.raw_path`), and run one live test with a real implementation (Node's `EventSource`, a bare git repo, the real srt). |
| **A boundary's config inside the boundary** | 09b: the sandbox's own settings file was written to the task dir, which the sandboxed command can write, so a leftover process could rewrite the next command's rules. | For every file that configures a security boundary, ask "can the bounded process write this path?" It must live where it cannot (here, under `data_dir`). |
| **Platform state machines** | 15a: Gaia opens PRs as drafts, and GitHub refuses to merge a draft. Each step was correct alone; together every merge would fail. | When the code puts something into a non-default state (draft, locked, paused), find the step that takes it out, and test the whole sequence. |
| **The irreversible fact rolled back with its cleanup** | 15a: a merge and the branch delete shared one transaction, so a failed delete un-recorded a real merge, and the resumed node then skipped the cleanup for good. | Commit what cannot be undone (a merge, a payment, a push) in its own transaction **before** the steps that can fail. Make the resumed path finish the cleanup. |
| **Network inside a DB transaction** | 15a: a push of up to 120 s and several API calls ran inside `session_scope`. | Look for `await client.` or `await git.` between `async with session_scope` and its end. Structure as load → network → persist. |
| **Cross-language contracts** | 16: event names and the CSP were hard-coded in both TypeScript and Python. | Give each shared value one JSON source of truth, and a backend test that compares it with the enum or constant. |
| **"My task" is the caller's task** | 10b and 10c: both runtimes registered `asyncio.current_task()` so `cancel()` could stop "their" work, but that task is the **caller's** (the graph node). 10c then swallowed the `CancelledError` without `uncancel()`, leaving the caller permanently cancelling. | Cancellable work runs in a **child task** (`asyncio.create_task`); `cancel()` cancels the child, and `current_task().cancelling()` tells whose cancellation arrived. Grep for `current_task()`. |
| **A bent contract test** | 10b: the junior added a per-runtime branch to the shared contract suite so its runtime could raise where the others return `cancelled`. | Any `if runtime_name == …` that changes an **assertion** in a contract test is a red flag. The implementation conforms to the contract, never the reverse. |
| **Every tool call needs an answer** | 10b: after an invalid `finish`, the loop broke out of the turn, leaving other tool calls without results. OpenAI-compatible APIs return 400 on the next request. | In tool loops, check every `break`/`return` before all of a turn's `tool_call_id`s have a tool message. |
| **A harness's own limits** | 10c: the Agent SDK's CLI enforces `MCP_TOOL_TIMEOUT` per tool call, shorter than the longest command Gaia allows. | When wrapping a harness, list its own timeouts and limits, and verify each one live with a deliberately tiny value. |
| **Lifecycle gaps between packages** | Live E2E 1: cancelling a run (12a) left its pending approval in the inbox (16/14); deciding it resumed a dead graph, which crashed on a stale `route` left in state by a guard that returned `{}`. Every package's tests passed. | For every way a run ends (complete, fail, cancel, pause), ask what happens to its approvals, steps, ticket and worktree. A node that ends the run returns an explicit route, never `{}`. |
| **"Recent" served oldest first** | 17d: the "Decided" list fetched the default order (oldest first) with no status filter, so it showed the oldest 50, pending included. The handoff (mine) specified it. | For every list the UI shows, check the server's sort order and filters against the label. "Recent" needs a server-side newest-first order. |
| **Selection after a mutation** | 17d: after a decision the selection moved to `index + 1`; once the refetch dropped the decided item, the next one was skipped. | When a list item is acted on and leaves the list, test both refetch outcomes: the item gone, and still there (202, async). |
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
- **UI packages: run a real browser** against the **production build** (`vite build` then
  `vite preview --port 5173 --strictPort`, so the CSP meta tag is live and the `/api` proxy works),
  with the API on 8765 and realistic data. A copy of a live E2E data dir plus a few seeded rows is
  ideal. Use Playwright from the scratchpad (`npm i playwright@<2-week-old version>`), open
  `/#token=…`, and drive every view: data shown, navigation, hotkeys, one decision per API path.
  Fail on any console error, page error or unexpected HTTP 4xx/5xx. Look at the screenshots
  yourself; text checks miss layout and duplicated labels.
- Make the smoke script's navigation explicit (go to the top before searching a list): a script
  that acts on the wrong item reports a false UI bug, or hides a real one.

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
