# GitHub: token check, push, pull requests, merge

> Packages **15a** and **15b** · `gaia/services/{github,pull_requests,ci,pr_sync}.py`,
> `GitService.push`, `gaia/api/routes/pull_requests.py`, `gaia github check` · current as of 2026-11-14

## What it's for
When a run's steps are done and a human approves, Gaia pushes the agent's branch, opens a
**draft** pull request, and later merges it after a second, explicit human approval. This package
is that GitHub side (15a opens and merges, 15b watches CI and keeps PRs in sync); importing
issues is 15c.

## Files
| File | Role |
|---|---|
| `services/github.py` | `GitHubClient` (typed REST calls, error mapping, rate limits), `parse_github_remote`/`RepoSlug`, `check_token`, the error types (`MergeBlocked`, `RateLimited`, …) |
| `services/pull_requests.py` | `PullRequestService.open_pr` / `merge`, which implement the orchestrator's `PullRequests` protocol, and `build_pr_body` |
| `services/git.py` | `GitService.push`, `validate_push_url`, the askpass helper |
| `cli.py` | `gaia github check --repo <name>` |
| `scripts/dev_github_check.py` | lead's live check of a token against one repo |

## How it works

### The token
Gaia uses a **fine-grained personal access token**, limited to the repositories you choose. It is
stored in the Keychain as `GITHUB_TOKEN` (`gaia secrets set GITHUB_TOKEN`).

| Permission | Why |
|---|---|
| Metadata: read | look up the repo and its default branch |
| Contents: read and write | push `gaia/*` branches; delete them after merge |
| Pull requests: read and write | open, read and merge PRs; mark a draft ready |
| Commit statuses, Checks: read | CI status (15b) |
| Issues: read | issue import (15c) |

`check_token` probes each permission with one cheap GET. Pull-requests *write* cannot be probed
without writing, so the first `create_pr` is that check. `gaia github check --repo <name>` prints
the table and exits 1 if anything is missing. `open_pr` runs the same check before its first push
for a repository; a passing result is cached, and a failure is checked again next time.

### Pushing without exposing the token
`GitService.push(location, url=…, branch=…, token=…)`:
- refuses any branch outside `gaia/*`, any URL that is not `https://github.com/…`, and any URL
  with `@` (credentials);
- refuses to push if any git config **rewrites URLs** (`url.*.insteadOf`), because a rewrite could
  send the push, and the token with it, elsewhere;
- runs `git push --no-verify --porcelain <url> refs/heads/<b>:refs/heads/<b>`, with **no force**
  in any form, plus pins: `credential.helper=` (no keychain helper stores anything),
  `http.sslVerify=true`, `http.proxy=`, `http.extraHeader=`, `http.followRedirects=false`, and
  hooks off (`HARDENED_CONFIG`);
- gives git the token **only in that one process's environment**, as `GAIA_GIT_TOKEN`. Git asks the
  askpass helper (`{data_dir}/bin/gaia-askpass`, 0700, no secret inside), which echoes it. The
  token is never in argv, the URL, git config or a file.

### Opening the PR (`open_pr(run_id)`)
It runs in three phases, so no network call holds a database transaction:
1. **Load:** the run, ticket, repository, worktree pin, steps and the approved plan's summary.
   Build the title (`[gaia] <ticket title>`) and body.
2. **Network:** token check, push, `find_open_pr` (a resumed node reuses its PR), or
   `create_pr(draft=True)`.
3. **Persist:** upsert the `pull_request` row and publish `pr.opened` once.

`build_pr_body` renders the plan summary, the steps with status, attempts and test results, the
cost, and `Closes #<n>` for an issue-imported ticket (`external_ref = "github#<n>"`). Everything
in it is untrusted model or user text, so every `@` gets a zero-width joiner, which stops an agent
from pinging people.

### Merging (`merge(run_id, pr)`)
It is reachable only after the `approve_merge` gate. If the row is already `merged`, it returns at
once. Otherwise:
1. `get_pr`. If it is not yet merged, **mark the draft ready for review** (GraphQL; GitHub will not
   merge a draft), then `merge_pr(squash, sha=<worktree HEAD>)`. Passing the SHA means GitHub
   refuses (409 → `MergeBlocked`) if anyone pushed to the branch after the human approved.
   Branch protection refusals (405) are also `MergeBlocked`. There is **no retry and no force**.
2. Commit the merged state.
3. Delete the remote branch, best-effort and logged, then publish `pr.merged`.

### Watching CI (`watch_ci(run_id, pr)`, 15b)
It watches the **worktree's HEAD**, the commit Gaia pushed and the human will approve, never
whatever the PR branch points at later.
- Each poll reads the combined status and every check run (paginated), and `ci_rollup` reduces
  them to `passed`, `failed`, `pending` or `unknown`:
  - **failed:** any `failure`/`error` status, or a completed check concluding `failure`,
    `cancelled`, `timed_out`, `action_required`, `startup_failure` or `stale`;
  - **pending:** anything still running;
  - **passed:** everything finished, with `neutral` and `skipped` counting as passing;
  - **unknown:** the repo has **no CI** at all (GitHub then says "pending" with 0 statuses).
- On a **change**, it updates `pull_request.ci_status` and publishes one `ci.status` event. It
  never publishes on every poll.
- Polls start every 30 s and back off ×1.5 to 5 minutes; a change resets them.
- **No CI:** it waits 2 minutes for checks to appear, then records `unknown` and returns
  "passed". The merge gate is still a human.
- **Two hours** without a result → `failed` with "timed out waiting for CI". A rate limit pauses
  the loop; any other GitHub error fails the step.

### Keeping PRs in sync (`PRSyncService`, 15b)
Humans also act on GitHub directly: they review, close, merge and push. A background task (started
in the app lifespan, `settings.pr_sync_enabled`) refreshes every **open** PR every 60 s:
- the state, the head SHA, `review_rollup` (each reviewer's latest decisive review:
  `changes_requested` > `approved` > `none`) and the CI rollup **at the PR's head**;
- a changed row is saved and published as `pr.updated`.

Without a token it does nothing, silently. One PR failing doesn't stop the others; a rate limit
ends the round; being offline is a warning, not an error.

### The PR list (`GET /api/v1/pull-requests?state=&limit=`, 15b)
Newest first, scoped to the workspace, in **one** joined query. Each row has the PR (number, URL,
state, CI, review state, head SHA) plus its run, its run status, ticket title and repository
name. This feeds the PRs view (17e).

### The client
- One `httpx.AsyncClient` with GitHub's media type, API version, `User-Agent: gaia` and a bearer
  token. The token is registered for redaction.
- **Errors:** 401 → `GitHubAuthError`, 403 → `GitHubPermissionError`, 404 → `GitHubNotFound`; the
  message comes from GitHub's JSON, redacted and capped.
- **Rate limits** (429, or 403 with `x-ratelimit-remaining: 0`): wait `retry-after` or until
  `x-ratelimit-reset`, through an injectable `sleep`. Up to 3 times, else `RateLimited`.
- **5xx:** retried twice for **GET only**. A POST/PUT/DELETE may have been applied, so callers are
  idempotent instead.

## Why it's built this way
- **Drafts by default.** A Gaia PR is opened before CI runs and before the final human gate. A
  draft cannot be merged by accident in GitHub's UI either.
- **SHA-pinned merges.** The human approves a specific diff. Pinning the merge to that commit
  makes "approve" mean exactly that.
- **No startup check.** Gaia is local-first: it must start offline and without a token. The check
  runs where it matters, before a push.
- **Load → network → persist.** A push can take two minutes; holding a SQLite transaction that long
  blocks every other writer. Committing the merge before the branch delete means a failed cleanup
  can never hide a merge that happened.

## Changing it safely
- Never put the token in argv, a URL, a file or a log. Tests plant
  `github_pat_planted_…` and assert it is absent everywhere.
- `tests/github_fakes.GitHubFake` matches the **raw** request path, as sent on the wire. Keep it
  that way: matching httpx's decoded path hid an encoding bug once.
- Every GitHub-touching graph node must stay idempotent; nodes re-run from the top on resume.
- Tests: `test_github_client.py`, `test_github_remote.py`, `test_git_push.py` (real pushes to a
  local bare repo), `test_pull_requests.py`, `test_cli_github.py`.

Related: [git & processes](git-and-processes.md) · [doc 15](../implementation/15-github-integration.md) ·
[review 15a](../reviews/2026-11-14-review-15a.md).
