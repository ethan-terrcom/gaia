# 15 — GitHub Integration

## Goal
Push agent branches, open PRs, track CI and reviews, merge after approval, and
optionally import issues as tickets.

## Out of scope
A GitHub App or webhooks (local v1 polls instead). GitLab.

## Files
- `gaia/services/github.py`
- `gaia/orchestrator/nodes/{open_pr,watch_ci,merge}.py` (replacing the stubs from 12)
- `gaia/api/routes/pull_requests.py`

## Tasks
1. Token: `GITHUB_TOKEN` from the keychain. Document the required **fine-grained PAT** scopes, limited
   to the selected repos: Contents R/W, Pull requests R/W, Checks R, Commit statuses R, Issues R, Metadata R.
   Fail at startup with a clear message if a scope is missing (check with a cheap API call).
2. `push_branch(worktree)`: `git push origin <branch>` through `run_proc`. The token is passed with a
   one-shot `GIT_ASKPASS` helper script that reads it from an env var set for this process only.
   **Never** embed the token in the remote URL or in git config. Refuse any branch not matching `gaia/*`.
3. `open_pr`: title `[gaia] <ticket title>`. The body contains the plan summary, the step list, test
   results, a cost summary and `Closes #<n>` if the ticket was imported. Create it as a **draft** by default.
4. `watch_ci`: poll combined status and check runs every 30s with backoff. Publish `ci.*` events and
   update `pull_request.ci_status`. Done when every check finishes.
5. `merge`: only reachable after the `approve_merge` interrupt. Use squash merge through the API. It
   respects branch protection (if GitHub refuses, surface the reason and do not retry with force).
   Afterwards, delete the remote branch and mark the worktree for cleanup.
6. Background `PRSyncService`: every 60s, refresh the state, reviews and CI of open PRs that Gaia
   created, so PRs that humans comment on or close outside Gaia stay in sync.
7. `POST /api/v1/repositories/{id}/import-issues?label=gaia` creates tickets with an `external_ref`.
   Issue bodies are untrusted.
8. `GET /api/v1/pull-requests?state=` returns rows that link run, ticket, PR URL and CI status.

## Tests
- Use `respx` or `httpx` mocking for the GitHub API. Cover the happy path, a merge blocked by protection, and rate limiting (honour `retry-after`).
- The push helper never writes the token to disk outside a temp file with mode `0600` that is deleted
  in a `finally` (or, better, keep it in an env var only).

## Acceptance criteria
- [ ] On a sandbox GitHub repo, a full run opens a draft PR, shows CI status, and merges after approval.
