"""GitHub REST API client for doc 15a.

The rules that shape this module are all security rules:

- The token is a :class:`pydantic.SecretStr` and is registered for redaction in
  :meth:`GitHubClient.__init__`, so any log line that accidentally includes it is
  masked. It is never put in a URL, a query string or an exception message.
- Every response is parsed into a small frozen dataclass. Callers never see raw
  JSON, and an unparseable success response is refused rather than guessed at.
- Rate limits are honoured for every verb: a rate-limited request was not
  processed, so the wait/retry rule in the doc applies to ``GET``, ``POST``,
  ``PUT`` and ``DELETE`` alike. A 5xx on a mutating verb is *not* retried, because
  GitHub may have applied it; the callers above this module are idempotent instead.

``Pull requests R/W`` deliberately cannot be checked without writing a PR. The
first :meth:`GitHubClient.create_pr` call is the check; its 403 message names the
missing permission (see ``check_token``'s docstring).
"""

from __future__ import annotations

import asyncio
import re
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote, urlsplit

import httpx
from pydantic import SecretStr

from gaia.security.redaction import redact, register_secret
from gaia.services.git import strip_url_credentials

#: GitHub's current REST API media type and API version (doc 15a).
GITHUB_ACCEPT = "application/vnd.github+json"
GITHUB_API_VERSION = "2022-11-28"
GITHUB_USER_AGENT = "gaia"

#: Default base URL. Tests point this at a ``MockTransport`` with a fake host.
DEFAULT_GITHUB_API_URL = "https://api.github.com"

#: Per-request timeout, in seconds (doc 15a).
REQUEST_TIMEOUT_S = 30.0

#: How many times a rate-limited request may be retried (doc 15a).
MAX_RATE_LIMIT_RETRIES = 3

#: How many times a GET may be retried on a 5xx response, with 1s then 2s backoff.
GET_5XX_RETRIES = 2

#: The fallback wait when GitHub gives no Retry-After or reset time.
DEFAULT_RATE_LIMIT_WAIT_S = 60.0

#: Legal GitHub owner/name characters, as required by the handoff.
_REPO_PART = re.compile(r"^[A-Za-z0-9_.-]{1,100}$")


class UnsupportedRemote(ValueError):
    """``parse_github_remote`` was given a URL that is not a GitHub remote."""


class GitHubError(Exception):
    """An error response from the GitHub API.

    ``message`` is built from the JSON body's ``message`` and each
    ``errors[].message`` or ``errors[].code``, capped at 500 characters and passed
    through :func:`gaia.security.redaction.redact`. The token must never appear in
    an exception, a log line or an event.
    """

    def __init__(
        self,
        status: int,
        message: str,
        documentation_url: str | None = None,
    ) -> None:
        self.status = status
        self.message = redact(message)[:500]
        self.documentation_url = documentation_url
        super().__init__(self.message)


class GitHubAuthError(GitHubError):
    """A 401 from GitHub: the token is missing, malformed or expired."""


class GitHubPermissionError(GitHubError):
    """A 403 from GitHub that is not a rate limit."""


class GitHubNotFound(GitHubError):
    """A 404 from GitHub."""


class RateLimited(GitHubError):
    """GitHub rate-limited us and the required wait exceeds ``max_wait_s``."""

    def __init__(self, retry_after_s: float) -> None:
        self.retry_after_s = retry_after_s
        super().__init__(
            429,
            f"GitHub rate limit requires waiting {retry_after_s:.1f}s, "
            "which exceeds the configured maximum",
        )


class MergeBlocked(GitHubError):
    """A merge was refused by GitHub (405/409/422). Never retried with force."""

    def __init__(self, reason: str, *, status: int) -> None:
        self.reason = redact(reason)[:500]
        super().__init__(status, self.reason)


class GitHubSetupError(Exception):
    """A GitHub repository/token is not configured well enough to continue."""


@dataclass(frozen=True)
class RepoSlug:
    """The owner and repository name parsed from a remote URL."""

    owner: str
    name: str

    @property
    def full_name(self) -> str:
        """``owner/name``, GitHub's canonical repository handle."""
        return f"{self.owner}/{self.name}"

    @property
    def https_url(self) -> str:
        """The HTTPS clone URL, whatever the original remote's scheme was.

        Gaia pushes over HTTPS with the token, so an SSH or scp remote is normalised
        to this URL.
        """
        return f"https://github.com/{self.owner}/{self.name}.git"


def parse_github_remote(url: str) -> RepoSlug:
    """Parse a GitHub remote URL into a :class:`RepoSlug`.

    Accepted forms:

    - ``https://github.com/<owner>/<repo>`` and ``.../<repo>.git``, with or without
      credentials in the URL. Credentials are stripped with
      :func:`gaia.services.git.strip_url_credentials` and never kept;
    - ``git@github.com:<owner>/<repo>.git``;
    - ``ssh://git@github.com/<owner>/<repo>.git``.

    Owner and repository names must match ``^[A-Za-z0-9_.-]{1,100}$`` and must not
    be ``.`` or ``..``. Anything else raises :class:`UnsupportedRemote`.
    """
    if not isinstance(url, str) or not url:
        raise UnsupportedRemote("a GitHub remote URL must be a non-empty string")
    stripped = strip_url_credentials(url)

    # scp-style remote. Checked before urlsplit: ``git@github.com:o/r.git`` has no
    # URL scheme, and urlsplit would treat the whole string as a path.
    scp_prefix = "git@github.com:"
    if stripped.startswith(scp_prefix):
        path = stripped.removeprefix(scp_prefix)
        if path.startswith("/") or path.count("/") != 1 or not path.endswith(".git"):
            raise UnsupportedRemote(f"not a GitHub scp remote: {url!r}")
        owner, repo_with_suffix = path.split("/", 1)
        repo = repo_with_suffix.removesuffix(".git")
        return _validated_slug(owner, repo, url)

    parsed = urlsplit(stripped)
    if parsed.scheme not in {"https", "ssh"}:
        raise UnsupportedRemote(f"not a GitHub HTTPS or SSH remote: {url!r}")
    host = parsed.hostname
    if host != "github.com":
        raise UnsupportedRemote(f"not a github.com remote: {url!r}")
    if parsed.query or parsed.fragment:
        raise UnsupportedRemote(f"GitHub remote must not contain a query or fragment: {url!r}")

    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) != 2:
        raise UnsupportedRemote(f"not a GitHub owner/repository remote: {url!r}")
    owner, repo_with_suffix = parts
    if parsed.scheme == "https":
        repo = repo_with_suffix.removesuffix(".git")
    elif repo_with_suffix.endswith(".git"):
        repo = repo_with_suffix.removesuffix(".git")
    else:
        # Only the documented SSH form is accepted; refuse SSH remotes without
        # the repository suffix rather than guessing.
        raise UnsupportedRemote(f"not a GitHub SSH remote ending in .git: {url!r}")
    return _validated_slug(owner, repo, url)


def _validated_slug(owner: str, repo: str, original: str) -> RepoSlug:
    """Apply the owner/name character rules, then build the slug."""
    if owner in {".", ".."} or repo in {".", ".."}:
        raise UnsupportedRemote(f"GitHub owner and repository must not be . or ..: {original!r}")
    if not _REPO_PART.fullmatch(owner):
        raise UnsupportedRemote(f"invalid GitHub owner {owner!r} in {original!r}")
    if not _REPO_PART.fullmatch(repo):
        raise UnsupportedRemote(f"invalid GitHub repository {repo!r} in {original!r}")
    return RepoSlug(owner=owner, name=repo)


@dataclass(frozen=True)
class RepoInfo:
    """What ``GET /repos/{owner}/{repo}`` tells us."""

    default_branch: str
    private: bool
    permissions: Mapping[str, bool]


@dataclass(frozen=True)
class PullRequestInfo:
    """The subset of a GitHub PR row Gaia cares about."""

    number: int
    url: str
    state: str
    draft: bool
    head_sha: str
    head_ref: str
    base_ref: str
    #: GraphQL id; needed to mark a draft ready for review (REST cannot).
    node_id: str = ""


@dataclass(frozen=True)
class MergeInfo:
    """The result of a successful merge."""

    sha: str
    merged: bool


@dataclass(frozen=True)
class TokenCheck:
    """The result of :func:`check_token`."""

    ok: bool
    missing: tuple[str, ...]


def _response_json_or_error(response: httpx.Response) -> Any:
    """Decode a JSON response body, refusing an unparseable success response."""
    try:
        return response.json()
    except ValueError as exc:
        raise GitHubError(
            response.status_code,
            "GitHub returned an unparseable response body",
        ) from exc


def _response_error_payload(response: httpx.Response) -> dict[str, Any]:
    """Return the decoded JSON error body, or an empty dict when there is none."""
    try:
        payload = response.json()
    except ValueError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _body_message(payload: dict[str, Any]) -> str:
    """Build the human-readable part of an error from GitHub's JSON body."""
    parts: list[str] = []
    message = payload.get("message")
    if isinstance(message, str) and message.strip():
        parts.append(message.strip())
    errors = payload.get("errors")
    if isinstance(errors, list):
        for error in errors:
            if not isinstance(error, dict):
                continue
            for key in ("message", "code"):
                value = error.get(key)
                if isinstance(value, str) and value.strip():
                    parts.append(value.strip())
    if parts:
        return " ".join(parts)
    if isinstance(message, str):
        return message
    return "GitHub API error"


def _raise_for_status(response: httpx.Response) -> None:
    """Raise the appropriate :class:`GitHubError` for an error response."""
    payload = _response_error_payload(response)
    message = _body_message(payload)
    raw_documentation_url = payload.get("documentation_url")
    documentation_url = raw_documentation_url if isinstance(raw_documentation_url, str) else None
    status = response.status_code
    if status == 401:
        raise GitHubAuthError(status, message, documentation_url)
    if status == 403:
        raise GitHubPermissionError(status, message, documentation_url)
    if status == 404:
        raise GitHubNotFound(status, message, documentation_url)
    raise GitHubError(status, message, documentation_url)


class GitHubClient:
    """A small, typed async client for the GitHub REST endpoints doc 15a uses.

    One :class:`httpx.AsyncClient` is created per instance; call
    :meth:`aclose` or use ``async with`` to release it.
    """

    def __init__(
        self,
        token: SecretStr,
        *,
        base_url: str = DEFAULT_GITHUB_API_URL,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        max_wait_s: float = 60.0,
        now: Callable[[], float] = time.time,
    ) -> None:
        register_secret(token.get_secret_value())
        self._token = token
        self._sleep = sleep
        self._now = now
        self.max_wait_s = max_wait_s
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            transport=transport,
            timeout=REQUEST_TIMEOUT_S,
            headers={
                "Accept": GITHUB_ACCEPT,
                "X-GitHub-Api-Version": GITHUB_API_VERSION,
                "User-Agent": GITHUB_USER_AGENT,
                "Authorization": f"Bearer {token.get_secret_value()}",
            },
        )

    async def aclose(self) -> None:
        """Close the wrapped ``httpx.AsyncClient``."""
        await self._client.aclose()

    async def __aenter__(self) -> GitHubClient:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: Any,
    ) -> None:
        await self.aclose()

    # ------------------------------------------------------------------ core --
    @staticmethod
    def _repo_path(slug: RepoSlug) -> str:
        """``/repos/<owner>/<repo>`` with URL-safe components."""
        return f"/repos/{quote(slug.owner, safe='')}/{quote(slug.name, safe='')}"

    @staticmethod
    def _is_rate_limited(response: httpx.Response) -> bool:
        if response.status_code == 429:
            return True
        return response.status_code == 403 and response.headers.get("x-ratelimit-remaining") == "0"

    def _rate_limit_wait_s(self, response: httpx.Response) -> float:
        """How long GitHub asks us to wait, or a safe fallback."""
        retry_after = response.headers.get("retry-after")
        if retry_after is not None:
            try:
                return float(retry_after)
            except ValueError:
                pass
        reset = response.headers.get("x-ratelimit-reset")
        if reset is not None:
            try:
                return max(0.0, float(reset) - self._now())
            except ValueError:
                pass
        return DEFAULT_RATE_LIMIT_WAIT_S

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, str] | None = None,
        json_body: Mapping[str, Any] | None = None,
    ) -> httpx.Response:
        """Send one request, applying the doc's rate-limit and 5xx retry rules."""
        rate_attempts = 0
        get_5xx_attempts = 0
        while True:
            response = await self._client.request(
                method, path, params=params, json=dict(json_body) if json_body is not None else None
            )
            if self._is_rate_limited(response):
                wait_s = self._rate_limit_wait_s(response)
                if rate_attempts >= MAX_RATE_LIMIT_RETRIES or wait_s > self.max_wait_s:
                    raise RateLimited(wait_s)
                rate_attempts += 1
                await self._sleep(wait_s)
                continue
            if (
                method == "GET"
                and 500 <= response.status_code < 600
                and get_5xx_attempts < GET_5XX_RETRIES
            ):
                get_5xx_attempts += 1
                await self._sleep(1.0 if get_5xx_attempts == 1 else 2.0)
                continue
            return response

    async def _request_json(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, str] | None = None,
        json_body: Mapping[str, Any] | None = None,
    ) -> Any:
        """Send a request and return decoded JSON, raising on error statuses."""
        response = await self._request(method, path, params=params, json_body=json_body)
        if response.status_code >= 400:
            _raise_for_status(response)
        return _response_json_or_error(response)

    # -------------------------------------------------------------- methods --
    async def get_repo(self, slug: RepoSlug) -> RepoInfo:
        """``GET /repos/{owner}/{repo}``."""
        payload = await self._request_json("GET", self._repo_path(slug))
        if not isinstance(payload, dict):
            raise GitHubError(200, "GitHub returned an unparseable repo response")
        default_branch = payload.get("default_branch")
        private = payload.get("private")
        if not isinstance(default_branch, str) or not default_branch:
            raise GitHubError(200, "GitHub returned a repo without a default branch")
        if not isinstance(private, bool):
            raise GitHubError(200, "GitHub returned a repo without a private flag")
        permissions = payload.get("permissions")
        if not isinstance(permissions, dict):
            permissions = {}
        return RepoInfo(
            default_branch=default_branch,
            private=private,
            permissions={
                key: value for key, value in permissions.items() if isinstance(value, bool)
            },
        )

    async def find_open_pr(self, slug: RepoSlug, head_branch: str) -> PullRequestInfo | None:
        """``GET /repos/{owner}/{repo}/pulls?head=owner:branch&state=open``."""
        payload = await self._request_json(
            "GET",
            f"{self._repo_path(slug)}/pulls",
            params={
                "head": f"{slug.owner}:{head_branch}",
                "state": "open",
                "per_page": "5",
            },
        )
        if not isinstance(payload, list):
            raise GitHubError(200, "GitHub returned an unparseable pulls response")
        for item in payload:
            if not isinstance(item, dict):
                raise GitHubError(200, "GitHub returned an unparseable pull request")
            return _parse_pull_request(item)
        return None

    async def create_pr(
        self,
        slug: RepoSlug,
        *,
        head: str,
        base: str,
        title: str,
        body: str,
        draft: bool = True,
    ) -> PullRequestInfo:
        """``POST /repos/{owner}/{repo}/pulls``, as a draft by default."""
        payload = await self._request_json(
            "POST",
            f"{self._repo_path(slug)}/pulls",
            json_body={
                "head": head,
                "base": base,
                "title": title,
                "body": body,
                "draft": draft,
            },
        )
        if not isinstance(payload, dict):
            raise GitHubError(200, "GitHub returned an unparseable pull request")
        return _parse_pull_request(payload)

    async def get_pr(self, slug: RepoSlug, number: int) -> PullRequestInfo:
        """``GET /repos/{owner}/{repo}/pulls/{number}``."""
        payload = await self._request_json("GET", f"{self._repo_path(slug)}/pulls/{number}")
        if not isinstance(payload, dict):
            raise GitHubError(200, "GitHub returned an unparseable pull request")
        return _parse_pull_request(payload)

    async def merge_pr(
        self,
        slug: RepoSlug,
        number: int,
        *,
        sha: str,
        commit_title: str,
    ) -> MergeInfo:
        """``PUT /repos/{owner}/{repo}/pulls/{number}/merge`` with squash.

        There is deliberately no force option and no retry: a merge is not
        idempotent enough to retry, and branch protection exists to be respected.
        """
        response = await self._request(
            "PUT",
            f"{self._repo_path(slug)}/pulls/{number}/merge",
            json_body={
                "merge_method": "squash",
                "sha": sha,
                "commit_title": commit_title,
            },
        )
        if response.status_code == 405:
            raise MergeBlocked(_body_message(_response_error_payload(response)), status=405)
        if response.status_code == 409:
            raise MergeBlocked("the PR head changed since it was approved", status=409)
        if response.status_code == 422:
            raise MergeBlocked(_body_message(_response_error_payload(response)), status=422)
        if response.status_code >= 400:
            _raise_for_status(response)
        payload = _response_json_or_error(response)
        if not isinstance(payload, dict):
            raise GitHubError(200, "GitHub returned an unparseable merge response")
        merged_sha = payload.get("sha")
        merged = payload.get("merged")
        if not isinstance(merged_sha, str) or not merged_sha:
            raise GitHubError(200, "GitHub returned a merge without a sha")
        if not isinstance(merged, bool):
            raise GitHubError(200, "GitHub returned a merge without a merged flag")
        return MergeInfo(sha=merged_sha, merged=merged)

    async def mark_ready_for_review(self, pr: PullRequestInfo) -> None:
        """Turn a draft PR into a normal one, so it can be merged.

        GitHub refuses to merge a draft ("Pull Request is still a draft", 405),
        and Gaia opens every PR as a draft. REST has no endpoint for this; the
        GraphQL ``markPullRequestReadyForReview`` mutation is the documented way
        (lead review G2). A PR that is not a draft is left alone.
        """
        if not pr.draft:
            return
        if not pr.node_id:
            raise GitHubError(200, f"pull request #{pr.number} has no node_id to mark ready")
        response = await self._request(
            "POST",
            "/graphql",
            json_body={
                "query": (
                    "mutation($id: ID!) { markPullRequestReadyForReview("
                    "input: {pullRequestId: $id}) { pullRequest { isDraft } } }"
                ),
                "variables": {"id": pr.node_id},
            },
        )
        if response.status_code >= 400:
            _raise_for_status(response)
        payload = _response_json_or_error(response)
        errors = payload.get("errors") if isinstance(payload, dict) else None
        if errors:
            raise GitHubError(200, _body_message({"errors": errors}))

    async def delete_branch(self, slug: RepoSlug, branch: str) -> None:
        """``DELETE /repos/{owner}/{repo}/git/refs/heads/{branch}``.

        404 and 422 count as already gone, so a resumed run can call this again.
        Only ``gaia/*`` branches are accepted: Gaia never deletes a human branch.
        """
        if not branch.startswith("gaia/"):
            raise ValueError(f"refusing to delete non-gaia branch {branch!r}")
        response = await self._request(
            "DELETE",
            # Slashes stay literal: the ref is ``heads/gaia/<id>-<slug>``, and an
            # encoded ``%2F`` 404s, which this method would read as "already gone"
            # (lead review G1).
            f"{self._repo_path(slug)}/git/refs/heads/{quote(branch, safe='/')}",
        )
        if response.status_code in {404, 422}:
            return
        if response.status_code >= 400:
            _raise_for_status(response)


def _parse_pull_request(payload: dict[str, Any]) -> PullRequestInfo:
    """Build a :class:`PullRequestInfo` from a decoded PR object."""
    number = payload.get("number")
    html_url = payload.get("html_url")
    draft = payload.get("draft")
    head = payload.get("head")
    base = payload.get("base")
    if not isinstance(number, int):
        raise GitHubError(200, "GitHub returned a pull request without a number")
    if not isinstance(html_url, str) or not html_url:
        raise GitHubError(200, "GitHub returned a pull request without an html_url")
    if not isinstance(draft, bool):
        raise GitHubError(200, "GitHub returned a pull request without a draft flag")
    if not isinstance(head, dict) or not isinstance(base, dict):
        raise GitHubError(200, "GitHub returned a pull request without head/base refs")
    head_sha = head.get("sha")
    head_ref = head.get("ref")
    base_ref = base.get("ref")
    if not isinstance(head_sha, str) or not head_sha:
        raise GitHubError(200, "GitHub returned a pull request without a head sha")
    if not isinstance(head_ref, str) or not isinstance(base_ref, str):
        raise GitHubError(200, "GitHub returned a pull request without head/base ref names")
    state = "merged" if payload.get("merged_at") is not None else payload.get("state")
    if state not in {"open", "closed", "merged"}:
        raise GitHubError(200, f"GitHub returned an unknown pull request state {state!r}")
    node_id = payload.get("node_id")
    return PullRequestInfo(
        number=number,
        url=html_url,
        state=state,
        draft=draft,
        head_sha=head_sha,
        head_ref=head_ref,
        base_ref=base_ref,
        node_id=node_id if isinstance(node_id, str) else "",
    )


async def check_token(client: GitHubClient, slug: RepoSlug) -> TokenCheck:
    """Check the fine-grained PAT permissions Gaia needs, one cheap call each.

    A 403 or 404 response means the permission is missing; any other error is
    raised. Metadata is checked first because the repository's default branch is
    needed for the commit-status and check-run endpoints.

    Pull-requests **write** cannot be checked without creating a PR. The first
    :meth:`GitHubClient.create_pr` call is the check, and its 403 message names
    the missing permission.
    """
    try:
        repo = await client.get_repo(slug)
    except (GitHubPermissionError, GitHubNotFound):
        return TokenCheck(ok=False, missing=("Metadata R",))

    missing: list[str] = []
    if not repo.permissions.get("push"):
        missing.append("Contents R/W")

    default_branch = quote(repo.default_branch, safe="")

    async def _require(label: str, path: str, params: Mapping[str, str] | None = None) -> None:
        try:
            await client._request_json("GET", path, params=params)
        except (GitHubPermissionError, GitHubNotFound):
            missing.append(label)

    await _require("Pull requests R", f"{client._repo_path(slug)}/pulls", {"per_page": "1"})
    await _require("Issues R", f"{client._repo_path(slug)}/issues", {"per_page": "1"})
    await _require(
        "Commit statuses R",
        f"{client._repo_path(slug)}/commits/{default_branch}/status",
    )
    await _require(
        "Checks R",
        f"{client._repo_path(slug)}/commits/{default_branch}/check-runs",
        {"per_page": "1"},
    )

    return TokenCheck(ok=not missing, missing=tuple(missing))
