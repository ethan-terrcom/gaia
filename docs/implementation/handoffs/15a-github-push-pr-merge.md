# Handoff: 15a — GitHub: token check, push, open PR, merge

Filled from `HANDOFF_TEMPLATE.md` for the lead's sandboxed DeepSeek harness (2026-11-14). Your file
tools are confined to the worktree. Your `run` tool is sandboxed: no network, no git writes, no
installs. The lead commits and merges; you never commit. Your report goes through `finish`.

---

You are a junior engineer on **Gaia**, a local-first control plane for AI coding agents. Your task
is to implement exactly one work package, **15a**, the GitHub side of a run:
- check a token's permissions;
- push an agent branch without ever writing the token to disk or argv;
- open a draft PR;
- merge it after a human approved.

CI watching, PR sync and the PR API are **15b**, and issue import is **15c**; do not build them.
Wiring these services into the graph is **12b**; you only provide them.

## 1. Read first, in this order
1. `docs/implementation/15-github-integration.md`: the split note and tasks 1, 2, 3 and 5.
2. `docs/implementation/00-README.md`, "Rules for every work package".
3. `docs/ARCHITECTURE.md` §6, especially "agents push only to `gaia/*`" and the hook rules.
4. `docs/backend/README.md`, `git-and-processes.md`, `config-and-secrets.md`, `database.md`,
   `events.md`, `python-concepts.md`.
5. `docs/reviews/2026-11-13-review-05b.md` (the F1 worktree pin and git hardening).
6. The code you build on:
   - `gaia/services/{git,proc,events,worktrees}.py`;
   - `gaia/config/{secrets,settings}.py`, `gaia/security/redaction.py`;
   - `gaia/db/{models,repo}.py` (`Repository`, `Ticket`, `Run`, `RunStep`, `Worktree`,
     `PullRequest`, `Approval`);
   - `gaia/domain/{enums,plans}.py`;
   - `gaia/orchestrator/deps.py` (the `PullRequests` protocol) and `nodes/{open_pr,merge}.py`;
   - `gaia/cli.py`;
   - `tests/git_repo.py`, `tests/conftest.py`, and an existing `httpx.MockTransport` test (grep
     `MockTransport` in `tests/`).

## 2. Lead notes for this package
These are design decisions already made. Follow them, and record any deviation in your report.

### 2.1 `gaia/services/github.py`: the API client
- `RepoSlug` is a frozen dataclass: `owner`, `name`, and a property `full_name`.
  `parse_github_remote(url: str) -> RepoSlug` accepts:
  - `https://github.com/<o>/<r>` and `https://github.com/<o>/<r>.git`, with or without
    credentials in the URL. Strip them with `strip_url_credentials` from `git.py`; never keep
    them;
  - `git@github.com:<o>/<r>.git`;
  - `ssh://git@github.com/<o>/<r>.git`.

  Owner and name must match `^[A-Za-z0-9_.-]{1,100}$`, and must not be `.` or `..`. Anything
  else, including other hosts, raises `UnsupportedRemote(ValueError)`. Gaia pushes over HTTPS
  with the token **whatever the remote's scheme**, so `RepoSlug.https_url` returns
  `https://github.com/<o>/<r>.git`.
- `GitHubClient(token: SecretStr, *, base_url: str = "https://api.github.com", transport:
  httpx.AsyncBaseTransport | None = None, sleep: Callable[[float], Awaitable[None]] =
  asyncio.sleep, max_wait_s: float = 60.0)`.
  - It wraps one `httpx.AsyncClient`; `aclose()` closes it, and it supports `async with`.
  - Headers: `Accept: application/vnd.github+json`, `X-GitHub-Api-Version: 2022-11-28`,
    `User-Agent: gaia`, `Authorization: Bearer <token>`.
  - Call `register_secret(token.get_secret_value())` from `gaia.security.redaction` in
    `__init__`, so a token that leaks into any log line is masked.
  - Timeout is 30 s per request.
- **Errors.** `GitHubError(Exception)` carries `status: int`, `message: str` and
  `documentation_url: str | None`. Build the message from the JSON body's `message` plus each
  `errors[].message` or `errors[].code`, capped at 500 characters, and pass it through
  `redact()`. The token must never appear in an exception, a log or an event. Subclasses:
  - `GitHubAuthError` for 401;
  - `GitHubPermissionError` for 403 when it is not a rate limit;
  - `GitHubNotFound` for 404;
  - `RateLimited`, raised when the wait would exceed `max_wait_s`.
- **Rate limits and retries** (doc 15 tests).
  - A 429, or a 403 with `x-ratelimit-remaining: 0`, is a rate limit.
  - Wait `retry-after` seconds if the header is present. Otherwise wait until
    `x-ratelimit-reset` (an epoch; get the clock from an injectable `now: Callable[[], float] =
    time.time`). Otherwise wait 60 s.
  - If the wait is at most `max_wait_s`, sleep through the injected `sleep` and retry, at most
    3 times. Otherwise raise `RateLimited(retry_after_s)`. This applies to **every** method,
    because a rate-limited request was not processed.
  - A 5xx is retried twice with backoff (1 s, then 2 s) **for GET only**. A POST, PUT or DELETE
    is never retried on 5xx, because it may have been applied; the callers below are idempotent
    instead.
- Methods. Return small frozen dataclasses, never raw JSON.
  - `get_repo(slug) -> RepoInfo(default_branch, private, permissions: Mapping[str, bool])`
    (`GET /repos/{o}/{r}`; `permissions` comes from the response's `permissions` object, or
    `{}`).
  - `find_open_pr(slug, head_branch) -> PullRequestInfo | None`
    (`GET /repos/{o}/{r}/pulls?head={o}:{branch}&state=open&per_page=5`).
  - `create_pr(slug, *, head, base, title, body, draft=True) -> PullRequestInfo`
    (`POST /repos/{o}/{r}/pulls`).
  - `get_pr(slug, number) -> PullRequestInfo`.
  - `merge_pr(slug, number, *, sha, commit_title) -> MergeInfo(sha, merged)`
    (`PUT /repos/{o}/{r}/pulls/{n}/merge` with `{"merge_method": "squash", "sha": sha,
    "commit_title": commit_title}`). Map the errors:
    - **405** → `MergeBlocked(reason)`: not mergeable, or branch protection. The reason is the
      API message;
    - **409** → `MergeBlocked("the PR head changed since it was approved")`;
    - **422** → `MergeBlocked(message)`.

    Never retry with any force option; none exists, and none should be invented.
  - `delete_branch(slug, branch)` (`DELETE /repos/{o}/{r}/git/refs/heads/{branch}`). 404 and
    422 count as already gone. Refuse a branch without the `gaia/` prefix.
  - `PullRequestInfo` is `number`, `url` (the `html_url`), `state` (`"open"`, `"closed"`,
    `"merged"`, where `merged_at` set means merged), `draft`, `head_sha`, `head_ref` and
    `base_ref`.
- **Token check** (task 1): `async def check_token(client, slug) -> TokenCheck(ok: bool,
  missing: tuple[str, ...])`. One cheap call per permission. A 403 or 404 response means
  missing; any other error is raised.

  | Permission (fine-grained PAT) | Check |
  |---|---|
  | Metadata R | `GET /repos/{o}/{r}` succeeds |
  | Contents R/W | that response's `permissions.push` is true |
  | Pull requests R | `GET /repos/{o}/{r}/pulls?per_page=1` |
  | Issues R | `GET /repos/{o}/{r}/issues?per_page=1` |
  | Commit statuses R | `GET /repos/{o}/{r}/commits/{default_branch}/status` |
  | Checks R | `GET /repos/{o}/{r}/commits/{default_branch}/check-runs?per_page=1` |

  Pull-requests **write** cannot be checked without writing. Document it: the first `create_pr`
  call is the check, and its 403 message names the missing permission.
- **When the check runs.** The doc says "fail at startup". A local-first app must still start
  with no GitHub token and no network, so:
  - (a) add the CLI command `gaia github check --repo <name>`, which prints a table of
    permissions and exits 1 if any are missing;
  - (b) `PullRequestService.open_pr` runs `check_token` once per process per repo, cached, before
    the first push. A missing permission raises `GitHubSetupError` with a message listing what is
    missing and the doc link, and the node fails with that message.

  Record this as a deviation.

### 2.2 `GitService.push` (in `gaia/services/git.py`): the only git change
`async def push(self, location: WorktreeLocation, *, url: str, branch: str, token: SecretStr,
timeout_s: float = 120) -> None`:
- Validate first:
  - `check_ref_argument(branch)`;
  - the branch must start with `BRANCH_PREFIX` (`gaia/`), otherwise `UnsafePath`/`InvalidRef`.
    Reuse what exists;
  - `url` must start with `https://github.com/` and contain no `@`, with no credentials in it.
- argv (after `HARDENED_CONFIG`, which `_invoke` adds): `-c credential.helper= -c
  core.askPass=<askpass> -c push.default=nothing push --no-verify --porcelain <url>
  refs/heads/<branch>:refs/heads/<branch>`. **No `--force`** in any form. An empty
  `credential.helper=` resets the helper list, so a credential helper such as macOS's
  `osxkeychain` never stores or offers anything.
- **The token travels only in the environment of this one git process:** `GIT_ASKPASS=<askpass>`,
  `GAIA_GIT_TOKEN=<token>` and `GIT_TERMINAL_PROMPT=0`, passed through `_command_context`'s env
  merged with the extra env. Never put it in argv, the URL, git config or a file.
- `<askpass>` is a helper script written once by `GitService` to `{data_dir}/bin/gaia-askpass`,
  mode `0o700`, rewritten if its content differs (the same pattern as `_write_config`). Its
  content is exactly:
  ```sh
  #!/bin/sh
  # Written by Gaia (doc 15a). Answers git's credential prompts from the environment only.
  case "$1" in
    Username*) printf '%s\n' x-access-token ;;
    *) printf '%s\n' "$GAIA_GIT_TOKEN" ;;
  esac
  ```
- A failed push raises `GitError`. Its message must not contain the token: `register_secret`
  covers logs, but add a test anyway.
- **Tests without network:**
  - push to a **local bare repo**. Create one with `tests/git_repo.py` helpers or `git init
    --bare` in `tmp_path`, via `subprocess` in tests only;
  - structure it so the strict URL rule and the real push are tested separately, with no
    test-only flag in production code:
    - a public `validate_push_url(url)` holds the URL rule;
    - a private `_push_argv(url, branch)` builds the argv;
    - a private `_push(location, *, url, branch, token, timeout_s)` does the work without the
      URL rule.

    `push()` calls `validate_push_url`, then `_push`. The bare-repo test calls `_push` with a
    `file://` URL; the URL tests call `validate_push_url` and `push` directly;
  - the askpass test runs the script with `run_proc`, with a planted `GAIA_GIT_TOKEN`, and checks
    the output for each prompt;
  - a pre-push hook in the bare repo's source worktree must **not** run. Plant a hook that
    writes a marker file, and assert the marker is absent.

### 2.3 `gaia/services/pull_requests.py`: `PullRequestService`
It implements the `open_pr` and `merge` parts of the orchestrator's `PullRequests` protocol.
`watch_ci` is 15b: leave `async def watch_ci(...)` raising `NotImplementedError("TODO(gaia-15b)")`,
and do not use it anywhere.

Constructor: `PullRequestService(*, sessions, git: GitService, secrets: SecretStore, bus:
EventBus, client_factory: Callable[[SecretStr], GitHubClient] = GitHubClient)`.

**`async def open_pr(self, run_id: str) -> int`** must be idempotent, because a resumed graph node
re-runs from the top:
1. Load the run, ticket, repository and worktree rows (scoped by the run's workspace) and the
   `run_step` rows.
2. `repository.remote_url` must parse (`parse_github_remote`), otherwise `GitHubSetupError("this
   repository has no GitHub remote")`.
3. `token = secrets.require(SecretName.GITHUB_TOKEN)`.
4. Run the cached `check_token`.
5. Build the `WorktreeLocation` through `WorktreeService.location(worktree)`, then push
   `worktree.branch`.
6. `existing = find_open_pr(slug, branch)`. If found, use it. Otherwise call
   `create_pr(head=branch, base=repository.default_branch, title=f"[gaia] {ticket.title}"[:250],
   body=build_pr_body(...), draft=True)`.
7. **Upsert** the `pull_request` row, keyed by `(run_id, number)`, with `url`, `state` and
   `ci_status="pending"`.
8. Publish `EventType.PR_OPENED` `{run_id, number, url}` only when the row was newly created.
9. Return the number.

**`build_pr_body(...) -> str`** is a pure function:
- the plan summary, from the most recent **approved** `plan` approval's `payload_json["plan"]
  ["summary"]` if present; check `gaia/domain/plans.py` for the field name;
- a numbered step list (`title`, `status` and `attempts`);
- the test results per step, from `run_step.result_summary` if present, else "not recorded";
- the run cost (`run.cost_usd`, two decimals);
- `Closes #<n>` when `ticket.external_ref` matches `^github#(\d+)$`. That is the format 15c will
  write. Document it in the function's docstring.

Everything from the ticket or the steps is **untrusted text**: render it in plain Markdown, and
neutralise `@` mentions by inserting a zero-width joiner after `@`, so an agent-written summary
cannot ping people. Cap the body at 60,000 characters.

**`async def merge(self, run_id: str, pr: int) -> None`** is only reachable after the
`approve_merge` interrupt, and is idempotent:
1. Load the PR row and the worktree.
2. `info = get_pr(slug, pr)`. If `info.state == "merged"`, update the row and return.
3. Call `merge_pr` with **`sha=` the worktree's current HEAD**, the commit Gaia pushed and the
   human approved. If anyone pushed another commit to the branch since, GitHub returns 409, and
   Gaia refuses to merge it.
4. `MergeBlocked` propagates with its reason. The node fails, and the reason reaches the UI
   through the guard's event. No retry.
5. On success: set the row's `state="merged"`, call `delete_branch`, publish
   `EventType.PR_MERGED`, and leave `# TODO(gaia-18c): mark the worktree for cleanup`.

### 2.4 CLI
`gaia github check --repo <name>` (§2.1). Follow the existing Typer patterns in `gaia/cli.py`:
the session, workspace resolution and secret store usage. It must never print the token. Output
is a table of `permission  ok|missing`, and the exit code is 1 when anything is missing.

### 2.5 What you cannot prove (the lead will)
You have no network, so the real GitHub API and a real push are the lead's job. Make that easy:
- add `scripts/dev_github_check.py <owner/name>`, which reads `GITHUB_TOKEN` from the secret
  store and prints `check_token`'s result;
- use `httpx.MockTransport` everywhere else. The `transport=` parameter exists for exactly this.
  Do **not** add `respx`; no new dependencies.

## 3. Files
**You may create:**
- `gaia/services/github.py`, `gaia/services/pull_requests.py`;
- `scripts/dev_github_check.py`;
- `tests/test_github_client.py`, `tests/test_github_remote.py`, `tests/test_git_push.py`,
  `tests/test_pull_requests.py`, `tests/test_cli_github.py`, `tests/github_fakes.py` (a small
  scriptable `MockTransport` handler).

**You may edit:**
- `gaia/services/git.py` (`push`, `validate_push_url` and the askpass helper only) and its tests;
- `gaia/cli.py` (the `github` command group only);
- `docs/implementation/15-github-integration.md`: add "Decisions and deviations (as built) — 15a"
  at the end, including the exact PAT permissions table.

**You must not touch:**
- `pyproject.toml`, `uv.lock` and every `[tool.*]` section;
- `.github/`, migrations, `gaia/api/`, `gaia/orchestrator/` (the nodes keep calling the protocol;
  12b wires your service in);
- `gaia/security/` and `gaia/tools/` (a parallel package, 09b, is editing those);
- the other docs.

Another junior is building **09b** at the same time. It adds `GitAuthor` and `commit(author=)` to
`git.py`. Keep your `git.py` change to new functions only, so the lead's merge is clean.

## 4. Non-negotiable rules
- **The token never appears** in argv, a URL, git config, any file, a log line, an event, an exception message or a test snapshot. Tests use an obviously fake token
  such as `github_pat_planted_0000…` and assert it is absent from every captured output.
- Agents push only `gaia/*` branches. Force-push is impossible by construction.
- No `shell=True` and no `subprocess` outside tests; only `gaia/services/proc.py` spawns
  (`GitService._invoke` already uses it).
- SQL only through `gaia/db/` repositories and scoped helpers.
- Check `httpx` APIs against the installed source in `backend/.venv/lib/python3.12/site-packages/
  httpx/`, especially `MockTransport` and `AsyncClient` with `transport=`.
- Fail closed: no remote, no token, a missing permission, or an unparseable response each
  refuses, with a clear message.
- mypy strict. No new `# type: ignore` or `# noqa` without a reason comment.

## 5. How to work
1. Read §1, then write a short plan as your first message.
2. Order: remote parsing → client and errors → rate limits → `check_token` → `GitService.push`
   and askpass → `build_pr_body` → `PullRequestService` → CLI → dev script.
3. **Required tests** (each behaviour must be covered):
   - remote parsing: all three accepted forms, credentials stripped, other hosts and bad names
     refused;
   - client: the headers (including the bearer token) are sent; the error mapping for 401, 403,
     404, 405, 409, 422 and 500; the token never appears in `str(exc)`;
   - rate limits: 429 with `retry-after` sleeps then retries; 403 with remaining 0 uses
     `x-ratelimit-reset` through the injected clock; a wait over `max_wait_s` raises
     `RateLimited`; a 5xx on GET retries but on POST does not;
   - `check_token`: all present; each one missing in turn;
   - push: to a local bare repo succeeds and the branch exists there; a non-`gaia/` branch is
     refused; a URL with credentials, or a non-GitHub one, is refused by `validate_push_url`; the
     argv has no token and no `--force`; the env has the token only under `GAIA_GIT_TOKEN`; a repo
     pre-push hook does not run; the askpass script's output; the token is absent from a failed
     push's `GitError`;
   - `open_pr`: creates a draft PR with the right title, base and head, and writes the row and
     event; a second call reuses the existing PR, with no second POST and no second event; no
     remote → `GitHubSetupError`; a missing permission → `GitHubSetupError` naming it; no token →
     `MissingSecretError`;
   - `build_pr_body`: plan summary, steps, cost, `Closes #12`, @-mention neutralised, the cap;
   - `merge`: sends the worktree HEAD as `sha`; already merged returns early; a 405 becomes
     `MergeBlocked` with no retry; success deletes the branch and publishes `pr.merged`;
   - CLI: exit 0 with all present, exit 1 with one missing, and the token absent from output.
4. Checks, all of which must pass (run them from `backend/`):
   - `uv run ruff check .`
   - `uv run ruff format --check .`
   - `uv run mypy`
   - `uv run pytest -q`, at 100% coverage.

## 6. When blocked
Do not guess at architecture. Write the question under "Open questions", choose the most
restrictive reasonable behaviour, and keep going.

## 7. Report (pass to `finish`, in Markdown, with these headings)
- **Summary**
- **Files changed**
- **Deviations** from this handoff or doc 15, each with a reason
- **External APIs used**: the GitHub REST endpoints (with their docs.github.com URL) and the httpx
  calls, with the installed file you checked
- **Security-relevant changes**
- **Tests added**
- **Open questions**
- **Final check output**: the last lines of each check
