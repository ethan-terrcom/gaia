# 15 — GitHub Integration

**Split (2026-11-12 review):** **15a** covers the token check, `push_branch`, `open_pr` and `merge`
(tasks 1–3 and 5). **15b** covers `watch_ci`, `PRSyncService` and the PR API (tasks 4, 6 and 8).
**15c** covers issue import (task 7, optional in v1).

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
   Use `GitService`'s hardened environment, which disables hooks, so a repo `pre-push` hook never runs.
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

## Decisions and deviations (as built) — 15a

- **Token check timing.** The doc says "fail at startup". A local-first app must still start with no
  GitHub token and no network, so 15a does not block startup. Instead:
  1. `gaia github check --repo <name>` performs the check on demand and exits 1 when a permission is
     missing.
  2. `PullRequestService.open_pr` runs `check_token` before the first push for a repository. A
     **passing** check is cached for the process; a failing one is not (lead review P3). A missing permission raises `GitHubSetupError` listing the missing permissions
     and the doc link, and the node fails with that message.
- **Pull requests write is uncheckable without writing.** The first `create_pr` call is the check; its
  403 message names the missing permission. This is documented in `check_token` and
  `gaia/services/github.py`.
- **`watch_ci` is 15b.** `PullRequestService.watch_ci` raises `NotImplementedError("TODO(gaia-15b)")`
  and is not wired anywhere.
- **`merge_pr` commit title.** ~~`Merge PR #<number>`~~. Lead review: `[gaia] <ticket title> (#<n>)`,
  so `main`'s history reads like the PR list.
- **`delete_branch` refuses non-`gaia/*` branches.** A resumed merge can delete an already-deleted
  branch because 404 and 422 count as already gone.
- **`GitHubClient` clock.** The handoff calls for an injectable `now` in the rate-limit rules; the
  constructor therefore has one extra optional parameter (`now: Callable[[], float] = time.time`)
  after `max_wait_s`.

### Required fine-grained PAT permissions

| Permission | Check |
|---|---|
| Metadata R | `GET /repos/{owner}/{repo}` succeeds |
| Contents R/W | that response's `permissions.push` is true |
| Pull requests R | `GET /repos/{owner}/{repo}/pulls?per_page=1` |
| Issues R | `GET /repos/{owner}/{repo}/issues?per_page=1` |
| Commit statuses R | `GET /repos/{owner}/{repo}/commits/{default_branch}/status` |
| Checks R | `GET /repos/{owner}/{repo}/commits/{default_branch}/check-runs?per_page=1` |

Pull requests **write** is not in the table because it cannot be checked without creating a PR.

**Classic tokens.** Gaia expects a fine-grained PAT limited to the selected repositories. A classic
token with the `repo` scope also passes these checks, but it reaches every repository the owner
can access. The user guide recommends fine-grained tokens only.

### Lead review changes — 15a (2026-11-14, [`review-15a`](../reviews/2026-11-14-review-15a.md))
- **G2 (high):** Gaia opens every PR as a draft, and GitHub refuses to merge a draft (405 "Pull
  Request is still a draft"), so every merge would have failed. `merge` now calls
  `GitHubClient.mark_ready_for_review` first. REST cannot do this; it is the GraphQL
  `markPullRequestReadyForReview` mutation on the PR's `node_id`.
- **G1 (high):** `delete_branch` URL-encoded the `/` in `gaia/<id>` as `%2F`. That would make
  GitHub's refs endpoint 404, which the method reads as "already gone", so branches were silently
  never deleted. The test fake matched httpx's *decoded* path, which hid it. The fake now matches
  the raw wire path.
- **P2 (high):** `merge` committed the merged state in the same transaction as the branch delete,
  and returned early when GitHub already showed it merged. A failed delete rolled back the row, and
  a resumed node then skipped the delete and the `pr.merged` event for good. The merged state is
  now committed first. The delete is best-effort and logged. A resumed pass finishes the job, and
  a pass that finds the row recorded makes no GitHub call.
- **P1:** `open_pr` and `merge` no longer hold a DB transaction across the push and the API calls.
  They run as load → network → persist.
- **P4:** the push pins `http.sslVerify=true`, `http.proxy=`, `http.extraHeader=` and
  `http.followRedirects=false`, and refuses to push if any git config has a
  `url.*.insteadOf`/`pushInsteadOf` rewrite. The trusted repository's own config is still read
  during a push, and these are the settings that could send the token elsewhere.
- **P3:** only passing token checks are cached.
- Verified live against `api.github.com` and `github.com` with an obviously fake token. The API maps
  401 to `GitHubAuthError`, without the token in the message. The push reached GitHub through the
  askpass helper ("Invalid username or token"), failed without prompting, and kept the token out
  of the `GitError`.
- [ ] Acceptance ("a full run opens a draft PR, shows CI and merges after approval") needs 12b, 15b
  and an owner-provided sandbox repo with a fine-grained PAT.
