# Gaia v1: completion report

> **For:** the owner and Chief Architect · **From:** the lead engineer (Claude) · **Date:**
> 2026-11-15
> **In one line:** every implementation package (01–18) is built, reviewed, live-verified and on
> `main`. Gaia runs a ticket from plan to commit with real models in a real sandbox, and from plan
> to the PR approval in a free demo mode. Five decisions are yours (§5).

## 1. What Gaia is now
A local control plane on your Mac. You write a ticket, then:
1. Claude plans it and you approve the plan;
2. DeepSeek implements it in an isolated git worktree, inside an OS sandbox;
3. Gaia runs your tests and Claude reviews the work;
4. you approve opening a PR, Gaia watches CI, and you type `merge <n>` to merge.

Everything irreversible waits for you. A browser UI shows it all live:
- dashboard;
- tickets;
- a run's timeline and diff;
- the approvals inbox;
- PRs;
- worktrees.

**For a first-time user:** [`docs/USER_GUIDE.md`](../USER_GUIDE.md). `make demo` shows a full run
in a few minutes with no API keys and no cost.

## 2. How we know it works
| Evidence | Result |
|---|---|
| Automated tests | **1,899 backend tests at 100% line and branch coverage**, and 115 frontend tests. CI runs lint, strict typing, tests, a secret scan and a dependency audit on every PR. |
| **Live end-to-end run** (real Claude and DeepSeek, real sandbox, driven only through the API) | Plan → implement → sandboxed tests → review → commit, for **$0.068**. It found 4 bugs between packages that no unit test could see; all are fixed (#43). |
| **Live demo run** (`GAIA_FAKE_LLM=1`, real sandbox and git, in a real browser) | Start run → plan approval → implement, checks, review, commit → "Open PR?", in seconds, with no keys. The kill switch (Pause all → banner → Start run refused → Resume all) was checked the same way. |
| **Browser smoke test** of every view against real run data (production build, CSP on) | Every check passed, with no console, page or HTTP errors. |
| **Security review** (doc 18d) | All 10 checklist items hold, and each is now a test. Live: the sandbox blocks the network, `~/.ssh`, `~/.zshrc` and the Keychain. gitleaks over the whole history found nothing; 0 copies of a real key in any database, log or event. See [`SECURITY.md`](../SECURITY.md). |

**Not yet verified live:** opening, CI-watching and merging a **real GitHub PR**. That logic is
covered by tests against a GitHub fake whose request shapes were checked against GitHub's docs.
It needs a sandbox repository and a fine-grained token from you (decision 5.4).

## 3. Your conditions, and how they were met
- **"Easy to move away from Claude" (ADR-001).** Six portability rules, P1–P6, are enforced by
  tests:
  - vendor SDKs are confined to one adapter each;
  - roles map to runtimes through settings;
  - Gaia owns the tools, the state and the cost accounting.

  A **swap drill** test runs a complete ticket with **no Claude at all**.
- **Security first.** The decisions D1 (OS sandbox) and D2 (agents never hold a key; a gateway
  enforces budgets) are built and verified live.
- **Executive and detailed documentation:**
  - every package has a review in `docs/reviews/` that opens with a "Summary for the owner";
  - the implementation docs record "as built" decisions;
  - the guides live in `docs/backend/` and `docs/frontend/`;
  - there is the user guide and `SECURITY.md`.

## 4. How it was built
- **Method:** I wrote a handoff per package. A DeepSeek "junior" implemented it in a sandboxed
  worktree. I reviewed every line, fixed small bugs, flagged larger ones, verified live,
  documented, and merged through PRs with green CI. There was one branch per package and no
  stacked PRs.
- **Cost of the junior work:** 34 junior sessions, **$26–51 of DeepSeek tokens** in total (the range
  reflects cache-hit pricing), about 25 hours of unattended junior time, with 99% prompt-cache hits.
- **Where review mattered most.** Each of these passed the junior's own tests:
  - a draft PR could never have been merged (15a);
  - the live UI would never have updated (16);
  - the sandbox's own settings file was writable from inside the sandbox (09b);
  - a cancelled run's stale approval crashed the workflow (live E2E);
  - approving a daily-budget pause left it pending forever (18a).

  The bug classes are recorded in the `review-junior-package` skill and the ops guide, so the next
  packages avoid them.

## 5. Decisions for you
Each has my recommendation. None blocks using Gaia today.

1. **Repositories without a GitHub remote.** Today their runs end as `failed` at "open PR", even
   though the work is committed on the run's branch. **Recommend:** end them as `completed`, with
   a note naming the branch to merge yourself, and skip the PR and merge gates.
2. **Who takes a failing step's later attempts** (`lead_attempt_from = 3`: Claude takes the 3rd
   attempt). **Recommend:** keep it. It costs more only when DeepSeek has failed twice.
3. **Automatic worktree cleanup** is off by default (`GAIA_CLEANUP_ENABLED`). **Recommend:** keep
   it off. `gaia worktrees cleanup` is a safe dry run, and deleting checkouts on a timer should be
   your opt-in.
4. **A sandbox GitHub repository and a fine-grained token,** so I can verify PR → CI → merge
   live. **Recommend:** yes, a throwaway repository with branch protection.
5. **The sandbox policy questions from 09a:**
   - **D-a:** an owner-registered command outranks the built-in tripwires;
   - **D-b:** `git` is denied inside `run_command`.

   **Recommend:** confirm both as built.

**Also yours, from earlier:**
- confirm the budget defaults ($5 per run, $20 per day);
- move your keys from `~/.zshrc` into the Keychain with `gaia secrets set`, then rotate them;
- sign off the 18d security checklist;
- optional: on a plan or escalation, `r` rejects (ending the run) with no confirmation. Add one?
- the optional Docker runner (18c) is deferred.

## 6. Known follow-ups (small, none blocking)
- A cancelled or failed run leaves its in-flight step `running` and its ticket `in_progress`.
- The approvals list shows "Run <id>" rather than the ticket title.
- A 409 "ticket already has an active run" doesn't link to that run.
- On the Tickets page, `/` focuses the top-bar search rather than the page's own search box.
- A committed, self-contained Playwright smoke test, now that demo mode can generate its data.
