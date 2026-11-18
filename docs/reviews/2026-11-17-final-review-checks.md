# Review: the final review sees the step checks (2026-11-17)

**Branch:** `fix/final-review-checks`, after 19c (#73). Found by the owner on live runs.

## The problem
Both completed runs after 19c reached the PR gate with a final review of `revise` and the issue
"No `make test` output was provided, so the acceptance criterion "`make test` passes" cannot be
verified." The step's checks had run and passed. `final_review` sent the lead only the plan and the
full diff, and the review prompt treats missing test output as `revise`, so every run whose plan
says "tests pass" got this verdict. It dates from 12b-2, not from the agent modes.

## The fix
| Where | Change |
|---|---|
| `nodes/lead_final_review.py` | `latest_checks(state)`: each step's checks from its latest attempt, passed to the reviewer |
| `deps.Reviewer.final_review`, `impl/review.py` | take `checks: Mapping[int, CheckOutcome] | None` and pass them on |
| `lead.py` | `_final_check_sections`: one section per step, in order. Gaia's pass/fail line, then the output wrapped as untrusted. The last step's checks ran on the final code and keep the 20 KB cap; earlier steps keep 4 KB. No checks: says so |
| `prompts/lead_review.md` | the Tests rule explains the final review's test output, and not to ask for another run when the last step's checks passed |

## What was checked
| Check | Result |
|---|---|
| New tests | rendering order, statuses and truncation; the "none recorded" line; the node keeps the latest attempt per step and skips results without checks; the reviewer passes the checks into the model's message |
| `make check` | passes: 2093 backend tests at 100% coverage, 156 frontend tests |

## Open items
- None. Straight shot is unaffected: it skips the final review (19b B6).
