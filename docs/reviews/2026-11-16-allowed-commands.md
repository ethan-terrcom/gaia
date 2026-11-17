# Allowed commands, Approve + Log, and telling agents the commands (2026-11-16)

**The problem in the data:** the owner's live run on Gaia (01a1e205) asked for 15 approvals. 14 were
`npm` checks (`npm --prefix frontend test`, `run build`, `run lint`, `run typecheck`). After
approval, 10 of them passed in the sandbox. Three approvals expired unanswered, and two runs of
`npm test` failed in the repository root, which has no `package.json`. No agent ever ran
`npm install`. The policy asked because `npm` is a package manager, and the only command that
skips asking is the repository's single test command (`make test`), which was failing on the
venv bug that #64 fixed.

The owner chose options **B** (per-repository allowed commands) and **C** (tell the agent the
commands), plus a preload from past approvals and an **Approve + Log** button.

## What was built
- **`Repository.allowed_commands`** (migration `0006`): a JSON list of argv prefixes. They join
  the test command in the policy's `extra_allow`, so deny rules, the slashed-program rule and the
  bare-tripwire rule apply unchanged.
- **`allowed_entry_problem(entry)`** decides what may be stored. On top of the existing rules, it
  refuses a tripwire program with a code flag (`node -e`, `python -c`, `bash -c`), which would
  allow any inline script. **`suggest_allowed_prefix(argv)`** turns an approved command into an
  entry: words up to `--` or the first path/file/`key=value` operand, with trailing flags dropped.
- **Live effect.** `ToolContext.extra_allow_source` re-reads the run's repository for each
  command, so **Approve + Log** applies to the agent's very next command, not only from the next
  step.
- **`GET /repositories/{id}/command-suggestions`:** the prefixes of approved commands in this
  repository that would still ask. The **Edit commands** form lists them ticked ("preloaded");
  **Save commands** adds the ticked ones.
- **`DecideRequest.remember`.** The prefix is recomputed from the stored `argv` (never taken from
  the client) and committed in its own session before the agent is woken. It is audited as
  `repository.allow_command` after the broker's decision.
- **C.** Each step's instructions gain a **Repository commands** section: the test command, the
  allowed commands, and a note that setup already ran and that commands have no network. The
  section avoids a literal `Test command:` line, which the demo model parses.

## Decisions (lead's)
| # | Decision | Why |
|---|---|---|
| AC1 | Suggestions are ticked but need **Save**. They are not written silently by the migration. | The owner sees exactly what agents will be allowed to run. A data migration would also have to freeze the prefix rules. |
| AC2 | `node -e` / `python -c` style entries are refused, even though `extra_allow` alone would accept a two-word `node -e`. | They would mean "any code". Auto mode is the switch for that. |
| AC3 | Allowed commands count for agents' `run_command` only. Plan test commands still must start with the registered test command. | Plan validation and checks guard model output. Widening them was not asked for. |
| AC4 | Labelled **Approve + Log**, as the owner asked, with the prefix shown in plain text above the buttons and in the tooltip. | The owner can see what "Log" will add. |

## Verification
- `make check`: 1995 backend tests at 100% coverage, 135 frontend tests, lint, types, gitleaks.
- **Live (scratch instance on port 8799, real `srt`, demo LLM).** Run A asked for `uname -s`, and
  its approval offered the prefix `uname`. **Approve + Log** over HTTP returned 200; the
  repository now listed `[["uname"]]` and the audit rows were written. Run B ran `uname -s` with
  no approval row (decision `allow`).
- **Browser (Playwright, built UI on the same instance).**
  - The inbox shows **Approve + Log** and the prefix. Clicking it adds the entry.
  - **Edit commands** lists two suggestions, both ticked. Unticking one and saving stores only
    the other.
  - No console errors.
- **Read-only preview of the owner's database.** **gaia** would be offered
  `npm --prefix frontend test` (5×), `run build` (3×), `npm test` (2×), `run typecheck` and
  `run lint`. **greet** would be offered `python -m unittest` and `python3 -m unittest`.
  `npm test` from the root never worked in Gaia's repository, so untick it.

## Follow-ups
- A real bug found while testing: writing the audit row before the broker's decision locked
  SQLite ("database is locked"). The fix writes it after the decision, as the existing code
  already did.
- Agents still have no network in `run_command`. Adding a dependency mid-step will fail (option E,
  deferred until a ticket needs it).
