# 14 — Runs & Approvals API

## Goal
Expose the runs and the human-in-the-loop inbox over HTTP.

## Out of scope
UI (17).

## Files
- `gaia/api/routes/runs.py`
- `gaia/api/routes/approvals.py`
- `gaia/services/approvals.py`

## Endpoints
| Method | Path | Notes |
|---|---|---|
| POST | `/runs` | `{ticket_id, junior_runtime?, budget_usd?}` → `RunManager.start` |
| GET | `/runs?status=` | includes cost, current step and PR link |
| GET | `/runs/{id}` | state, steps, worktree, approvals, PR |
| POST | `/runs/{id}/cancel` | cancel the runtime task, mark the run cancelled. The worktree is kept |
| GET | `/runs/{id}/diff` | `git diff base..HEAD` of the worktree (capped) |
| GET | `/approvals?status=pending` | the inbox, oldest first |
| POST | `/approvals/{id}/decide` | `{decision: approve|reject|edit, comment?, edited_payload?}` |

## Tasks
1. `decide`:
   - Run it in a DB transaction that locks the approval row; a second decision gets 409.
   - Validate `edited_payload` against the schema for the approval kind (for example, the `Plan` model).
   - Record `decided_by`, the timestamp and an audit row. Publish `approval.decided`.
   - Call `RunManager.resume(run_id, decision)`.
2. **Merge approvals** need re-confirmation: the request must include `confirm: "merge <pr_number>"`.
   Anything else gets 422.
3. Approval kinds `command` and `budget` show the exact argv or budget figures in the payload. The UI must
   show the human exactly what they are approving.

## Tests
- Deciding twice gives 409. Deciding in another workspace gives 404.
- Approving a plan with an invalid edited plan gives 422.
- An end-to-end fake run, driven only through the HTTP API, reaches `completed`.

## Acceptance criteria
- [ ] A full run can be driven through the API alone, with `curl`.
