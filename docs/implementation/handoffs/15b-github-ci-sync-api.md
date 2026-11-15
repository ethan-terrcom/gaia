# Handoff: 15b — GitHub: CI watch, PR sync, PR API

Filled from `HANDOFF_TEMPLATE.md` for the lead's sandboxed DeepSeek harness (2026-11-14). Your file
tools are confined to the worktree. Your `run` tool is sandboxed: no network, no git writes, no
installs. The lead commits and merges; you never commit. Your report goes through `finish`.

---

You are a junior engineer on **Gaia**, a local-first control plane for AI coding agents. Your task
is to implement exactly one work package, **15b**:
- `watch_ci`, which waits for a PR's checks to finish;
- `PRSyncService`, a background task that keeps Gaia's view of its open PRs current when humans
  act on GitHub directly;
- `GET /api/v1/pull-requests`.

15a (client, push, open, merge) is merged, and you extend it. Issue import is 15c, and wiring
into the graph is 12b; do not build either.

## 1. Read first, in this order
1. `docs/implementation/15-github-integration.md`: tasks 4, 6 and 8, the "Tests" section, and the
   whole 15a "Decisions and deviations" section, including its lead review changes.
2. `docs/backend/github.md`: how 15a's client, errors, rate limits and idempotency work.
3. `docs/implementation/00-README.md`, "Rules for every work package".
4. `docs/backend/README.md`, `api.md`, `database.md`, `events.md`, `python-concepts.md`.
5. `docs/reviews/2026-11-14-review-15a.md`: the bug classes found there (a decoding fake, draft
   state, commit-before-cleanup).
6. The code you build on:
   - `gaia/services/{github,pull_requests,events}.py`;
   - `gaia/db/{models,repo}.py` (`PullRequest`, `Run`, `Ticket`), `gaia/domain/{enums,schemas}.py`
     (`CiStatus`, `PullRequestState`, `EventType.CI_STATUS`/`PR_UPDATED`);
   - `gaia/api/{app,deps}.py` (the lifespan and `AppServices.background_tasks`),
     `gaia/api/routes/approvals.py` (the newest list route, a good pattern);
   - `gaia/config/settings.py`, `alembic/versions/0004_run_tokens.py` (migration style);
   - `tests/github_fakes.py` (it matches the **raw wire path**; keep it that way),
     `tests/test_pull_requests.py`, `tests/test_github_client.py`.

## 2. Lead notes for this package
These are design decisions already made. Follow them, and record any deviation in your report.

### 2.1 New client methods (`gaia/services/github.py`)
Each one returns frozen dataclasses, and uses `_request_json`, which already handles errors, rate
limits and GET 5xx retries.
- `get_combined_status(slug, sha) -> CombinedStatus(state: str, total_count: int, statuses:
  tuple[CommitStatus(context, state), ...])`.
  - Endpoint: `GET /repos/{o}/{r}/commits/{sha}/status`.
  - `state` is one of `success`, `failure`, `error` or `pending`. GitHub reports `pending` when
    there are **no** statuses at all, so `total_count == 0` matters.
- `list_check_runs(slug, sha) -> tuple[CheckRun(name, status, conclusion | None), ...]`.
  - Endpoint: `GET /repos/{o}/{r}/commits/{sha}/check-runs?per_page=100`.
  - Follow pagination with `page=2`, `page=3` and so on while a page is full, up to 10 pages; the
    body has `total_count` and `check_runs`. `status` is `queued`, `in_progress`, `completed`,
    `waiting`, `requested` or `pending`.
- `list_reviews(slug, number) -> tuple[Review(user_login, state, submitted_at | None), ...]`.
  - Endpoint: `GET /repos/{o}/{r}/pulls/{n}/reviews?per_page=100`.
  - `state` is `APPROVED`, `CHANGES_REQUESTED`, `COMMENTED`, `DISMISSED` or `PENDING`.

`sha` must match `^[0-9a-f]{40}$`, and a `number` must be a positive int; otherwise raise
`ValueError`, so a bad value never reaches a URL.

### 2.2 Pure rollups (`gaia/services/ci.py`): unit-test these exhaustively
- `ci_rollup(status: CombinedStatus, checks: Sequence[CheckRun]) -> CiStatus`:
  - **failed** if any status is `failure` or `error`, **or** any completed check's conclusion is
    one of `failure`, `cancelled`, `timed_out`, `action_required`, `startup_failure` or `stale`;
  - otherwise **pending** if any check is not `completed`, or any status is `pending`;
  - otherwise **passed** if at least one status or check exists. `success`, `neutral` and `skipped`
    conclusions count as passing;
  - otherwise, with no statuses (`total_count == 0`) and no checks, **unknown**. The repository
    has no CI.
- `review_rollup(reviews) -> str`: take each user's **latest** review that is `APPROVED`,
  `CHANGES_REQUESTED` or `DISMISSED`. `COMMENTED` and `PENDING` reviews do not change a user's
  state. The result is:
  - `changes_requested` if any user's latest is `CHANGES_REQUESTED`;
  - else `approved` if any user's latest is `APPROVED`;
  - else `none`.

  Order by `submitted_at`; a review without `submitted_at` is ignored.

### 2.3 `PullRequestService.watch_ci(run_id, pr) -> str` (replaces the 15a stub)
The protocol in `gaia/orchestrator/deps.py` says it returns `"passed"` or `"failed"`. Keep that
return type, and decide:
1. Load the run's PR row, repo slug and worktree, the same way `merge` does (reuse `_load`). The
   SHA to watch is **the worktree's current HEAD**: what Gaia pushed and the human will approve,
   not whatever the branch points at later.
2. Loop, polling `get_combined_status` and `list_check_runs`, then `ci_rollup`:
   - on a change of rollup, update `pull_request.ci_status` in its own short transaction, then
     publish `EventType.CI_STATUS` `{run_id, number, sha, ci_status, checks: [{name, status,
     conclusion}…]}` with at most 50 checks. Publish only when the value changes, never on every
     poll;
   - `passed` → return `"passed"`; `failed` → return `"failed"`;
   - `pending` → sleep and poll again;
   - `unknown` (no CI) → keep polling for `settings.ci_no_checks_grace_s` (default 120 s), because
     checks often register a few seconds after a push. If still unknown, record `ci_status=
     "unknown"`, publish once, and return `"passed"`. The human gate after this is the real check,
     and a repo without CI must still be usable. Log `ci.no_checks`.
3. **Polling cadence:** start at `settings.ci_poll_interval_s` (default 30). After each poll with
   no change, multiply by 1.5, up to 300 s; a change resets it to the start value. The `sleep` and
   `monotonic` are injectable on the service (constructor keywords
   `sleep: Callable[[float], Awaitable[None]] = asyncio.sleep` and `monotonic: Callable[[],
   float] = time.monotonic`), so tests never wait.
4. **Overall limit:** `settings.ci_watch_timeout_s` (default 7200). On expiry, return `"failed"`
   after publishing `CI_STATUS` with `ci_status="failed"` and `reason: "timed out waiting for
   CI"`.
5. `RateLimited` from the client → sleep `min(exc.retry_after_s, 300)` and continue; it counts
   against the overall limit. Any other `GitHubError` propagates, the node fails with it, and the
   guard reports it.
6. **Idempotent across resume:** a re-run starts polling again from the current state.

### 2.4 `PRSyncService` (`gaia/services/pr_sync.py`)
- Constructor: `PRSyncService(*, sessions, secrets, bus, settings, client_factory=GitHubClient,
  sleep=asyncio.sleep)`.
- `async def sync_once(self) -> int`, for testing and `gaia dev`, returns how many rows changed:
  1. If `secrets.get(SecretName.GITHUB_TOKEN)` is `None`, return 0 and log `pr_sync.no_token` at
     debug level only, so a local-only user sees no warnings.
  2. Load every `pull_request` row whose `state` is `open`, across workspaces, through a new
     `PullRequestRepo.list_open()` in `gaia/db/repo.py`. This is a system-level read like
     `RunRepo.get_by_id`; follow that pattern and its docstring warnings. Join to `Run` and
     `Repository` to get the slug, and skip repos whose remote does not parse.
  3. For each row: `get_pr` (state, head SHA, draft), `list_reviews` → `review_rollup`, and the CI
     rollup at the PR's **head SHA** from GitHub. Sync reports what GitHub shows; `watch_ci` pins
     the approved SHA.
  4. If any of `state`, `ci_status`, `review_state` or `head_sha` changed, update the row in its
     own transaction and publish `EventType.PR_UPDATED` `{run_id, number, state, ci_status,
     review_state}` in the row's workspace.
  5. One PR's `GitHubError` is logged (`pr_sync.pr_failed`, with run_id and number) and skipped;
     the rest continue. `RateLimited` stops this round early.
- `async def run_forever(self)`:
  - loop `sync_once()`, then sleep `settings.pr_sync_interval_s` (default 60);
  - an unexpected exception is logged with `logger.exception("pr_sync.round_failed")`, and the loop
    continues after the interval. It never dies silently, and never spins in a tight loop;
  - `CancelledError` propagates, which is how the lifespan stops it.
- **Lifespan:** in `gaia/api/app.py`, when `settings.pr_sync_enabled` (default `True`), create the
  service, start `asyncio.create_task(service.run_forever(), name="pr-sync")` **after** the
  services exist, and add it to `services.background_tasks`. The existing `finally` cancels and
  awaits those tasks. Tests that build the app must not hit the network; the conftest's settings
  fixture should set `pr_sync_enabled=False`. Check how tests construct `Settings`, and put the
  default in the shared fixture, not in each test.

### 2.5 Migration `0005` (`--rev-id 0005`, down revision `0004`)
- Add `pull_request.review_state: String(32), nullable` and `pull_request.head_sha: String(64),
  nullable`.
- Use `op.batch_alter_table` for SQLite, as earlier migrations do. Read `0002`/`0003`.
- Update the `PullRequest` model with docstring comments.
- Add a migration round-trip test if the suite has a pattern for it; read
  `tests/test_migrations*.py`.

Another package may add `0006` later; yours is `0005`.

### 2.6 `GET /api/v1/pull-requests?state=&limit=` (`gaia/api/routes/pull_requests.py`)
- Workspace-scoped, newest first, `limit` 1–200 (default 100), and `state` an optional
  `PullRequestState`.
- Returns `PullRequestPage{items: list[PullRequestRead]}`. Each item is `{id, number, url, state,
  ci_status, review_state, head_sha, run_id, run_status, ticket_id, ticket_title, repo_name,
  created_at, updated_at}`. Join run, ticket and repository in **one query** in `gaia/db/repo.py`
  (`PullRequestRepo.list_with_context(workspace_id, state, limit)`), never N+1.
- Add the router to `api_v1_router`, then regenerate `frontend/src/api/openapi.json` with
  `uv run python scripts/export_openapi.py`. You cannot run `npm run gen:api`; the lead will.

### 2.7 Settings (`gaia/config/settings.py`)
Add `ci_poll_interval_s: float = 30` (> 0), `ci_watch_timeout_s: float = 7200` (> 0),
`ci_no_checks_grace_s: float = 120` (≥ 0), `pr_sync_enabled: bool = True` and
`pr_sync_interval_s: float = 60` (≥ 5). Each gets a docstring comment like its neighbours.

## 3. Files
**You may create:**
- `gaia/services/ci.py`, `gaia/services/pr_sync.py`, `gaia/api/routes/pull_requests.py`;
- `alembic/versions/0005_pull_request_review_state.py`;
- tests: `tests/test_ci_rollup.py`, `tests/test_watch_ci.py`, `tests/test_pr_sync.py`,
  `tests/test_api_pull_requests.py`, and additions to `tests/test_github_client.py`.

**You may edit:**
- `gaia/services/github.py` (new methods and dataclasses), `gaia/services/pull_requests.py`
  (`watch_ci` and constructor keywords);
- `gaia/db/models.py` (the two columns), `gaia/db/repo.py` (`PullRequestRepo`),
  `gaia/domain/schemas.py`;
- `gaia/api/app.py` (the router include and the lifespan task), `gaia/config/settings.py`;
- `tests/conftest.py` (`pr_sync_enabled=False` in the shared settings);
- `frontend/src/api/openapi.json` (regenerated only);
- `docs/implementation/15-github-integration.md`: add "Decisions and deviations (as built) — 15b".

**You must not touch:**
- `pyproject.toml`, `uv.lock` and every `[tool.*]` section;
- `.github/`, existing migrations, `gaia/orchestrator/`, `gaia/tools/`, `gaia/security/`,
  `gaia/runtimes/`;
- `frontend/` except `openapi.json`;
- other docs.

Two other juniors are building 10b and 10c (runtimes) in parallel. They don't touch your files.

## 4. Non-negotiable rules
- The token never appears in a log, an event, an exception or a test snapshot. Use the planted
  `github_pat_planted_…` style, and assert it is absent.
- Background work fails **loudly and safely**: logged, retried at the interval, never crashing
  the app, never spinning.
- SQL only in `gaia/db/`. No N+1 queries in the list route.
- Never write network calls inside a DB transaction (lead review 15a, P1): load → network →
  persist.
- Check httpx and SQLAlchemy APIs against the installed source.
- mypy strict; 100% coverage; no new `# type: ignore` or `# noqa` without a reason comment.

## 5. How to work
1. Read §1, then write a short plan as your first message.
2. Order: client methods → rollups → `watch_ci` → migration and model → `PullRequestRepo` → the
   API route → `PRSyncService` → lifespan → settings → openapi export.
3. **Required tests:**
   - each client method, with pagination for check runs and a bad SHA or number refused;
   - `ci_rollup`, as a table test covering every branch, including no-CI and neutral/skipped;
   - `review_rollup`: latest-per-user, comments ignored, a dismissal clearing an approval;
   - `watch_ci`:
     - pending → passed, with one event per change and none for repeated pending;
     - failed;
     - no checks, with grace then `passed` and `unknown`;
     - timeout → `failed`;
     - backoff growth and reset, through the injected sleep;
     - rate-limited, then continue;
     - it watches the **worktree HEAD**, not the PR head;
   - `PRSyncService.sync_once`:
     - no token → 0 with no calls;
     - a change in state, CI or reviews updates the row and publishes `pr.updated`;
     - no change → no event;
     - one PR failing doesn't stop the others;
     - closed and merged outside Gaia are picked up;
   - `run_forever`: an exception in a round is logged and the loop continues (inject `sleep` to stop
     after 2 rounds by raising `CancelledError`);
   - the lifespan starts the task when enabled and cancels it on shutdown; disabled → no task;
   - the API: scoping (another workspace's PR is invisible), the state filter, ordering, the joined
     fields, 401 without a token;
   - a migration round trip, if the suite has a pattern.
4. Checks, all of which must pass (run them from `backend/`):
   - `uv run ruff check .`
   - `uv run ruff format --check .`
   - `uv run mypy`
   - `uv run pytest -q` (100% coverage)

## 6. When blocked
Write the question under "Open questions", choose the most restrictive reasonable behaviour, and
keep going.

## 7. Report (pass to `finish`, in Markdown, with these headings)
- **Summary**
- **Files changed**
- **Deviations**, each with a reason
- **External APIs used**: the GitHub endpoints (docs.github.com URLs) and the library calls
- **Security-relevant changes**
- **Tests added**
- **Open questions**
- **Final check output**
