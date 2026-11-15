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
   - Graph gates (`plan`, `escalation`, `budget`, `pr_open`, `merge`): call `RunManager.resume(run_id, decision)`.
     `command` approvals: resolve the runtime's pending wait (doc 10, requirement 8). The graph is not resumed.
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

## Decisions and deviations (as built)

- **SQLite has no `SELECT ... FOR UPDATE`.** Double-decide protection is a
  process-wide `asyncio.Lock` per approval id, held across the read/validate/
  resume step and cleaned up after use. A second concurrent request sees either
  `RunBusy` or the already-decided row and gets 409.
- **`AppServices.runs` and `AppServices.approvals` were added here** with `None`
  defaults because 12b-3 had not landed on this branch. When either is `None`,
  the runs and decide routes return 503
  `{"error":{"code":"orchestrator_unavailable"}}`.
- **The merge confirmation reads `payload_json["pr_number"]` when present.**
  The 12a `approve_merge` node currently stores only `kind`, so the route falls
  back to the run's latest `pull_request` row. This keeps the typed confirmation
  working with the 12a fakes; 12b should add `pr_number` to the payload.
- **Command approvals are answered by `DbApprovalBroker.decide` and the graph is
  not resumed.** Graph gates return 202 because the resumed node applies the
  decision asynchronously.
- **The diff route never calls `diff_since`.** It runs
  `GitService.diff(location, base_sha, "HEAD")`, then a blank line, then
  `GitService.diff_worktree(location)`, outside any DB session. The combined
  output is capped at `max_bytes` and gets a trailing `\n[diff truncated]`.
- **Run summaries are one joined SQL statement** (`RunRepo.list_summary`): the
  ticket is joined, and the current step and PR fields are correlated scalar
  subqueries so the page limit applies to runs, not child rows.

### Lead review — 14 (2026-11-14, [`review-14`](../reviews/2026-11-14-review-14.md))
- **M1:** the `approve_merge` node now stores `pr_number` in the approval payload, so the inbox
  shows it and the typed confirmation (`merge <n>`) reads it directly. The route's fallback to
  the run's latest PR row is kept as defence in depth.
- The `AppServices.runs`/`approvals` fields the junior added locally were reconciled with 12b-3's
  at merge.
- [x] Acceptance ("a full run can be driven through the API alone, with `curl`"): verified live
  through the commit, in [live E2E run 1](../reviews/2026-11-14-live-e2e-1.md). The PR and merge
  steps need a GitHub sandbox repo.

### Live E2E fixes (2026-11-14, [`live-e2e-1`](../reviews/2026-11-14-live-e2e-1.md))
- `POST /approvals/{id}/decide` returns **409 `run_not_active`** when the approval's run is
  terminal, because deciding it would resume a graph that has ended.
- Cancelling a run, or the run failing, expires its pending approvals (`status=expired`) and
  publishes `approval.decided {action: "expired"}`.
- The `pr_open` approval payload carries the lead's `review`, so the human sees what they approve.
