"""Doc 15a GitHub client: headers, errors, rate limits, retries and check_token."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import httpx
import pytest
from pydantic import SecretStr

from gaia.services.github import (
    GitHubAuthError,
    GitHubClient,
    GitHubError,
    GitHubNotFound,
    GitHubPermissionError,
    MergeBlocked,
    PullRequestInfo,
    RateLimited,
    TokenCheck,
    check_token,
    parse_github_remote,
)
from tests.github_fakes import GitHubFake, json_response

TOKEN = "github_pat_planted_0000000000000000000000000000000000"
SLUG = parse_github_remote("https://github.com/acme/widgets.git")


def _client(
    fake: GitHubFake,
    *,
    sleep: Callable[[float], object] | None = None,
    now: Callable[[], float] | None = None,
    max_wait_s: float = 60.0,
) -> GitHubClient:
    """Build a client against a route-matching ``GitHubFake``."""

    async def _sleep(value: float) -> None:
        if sleep is not None:
            sleep(value)

    return GitHubClient(
        SecretStr(TOKEN),
        base_url="https://github.test",
        transport=httpx.MockTransport(fake),
        sleep=_sleep,
        max_wait_s=max_wait_s,
        now=now if now is not None else lambda: 1000.0,
    )


def _client_with_handler(
    handler: Callable[[httpx.Request], httpx.Response],
    *,
    sleep: Callable[[float], object] | None = None,
    now: Callable[[], float] | None = None,
    max_wait_s: float = 60.0,
) -> GitHubClient:
    """Build a client against a sequence-handler mock transport."""

    async def _sleep(value: float) -> None:
        if sleep is not None:
            sleep(value)

    return GitHubClient(
        SecretStr(TOKEN),
        base_url="https://github.test",
        transport=httpx.MockTransport(handler),
        sleep=_sleep,
        max_wait_s=max_wait_s,
        now=now if now is not None else lambda: 1000.0,
    )


def _repo_payload(*, push: bool = True) -> dict[str, Any]:
    return {
        "default_branch": "main",
        "private": True,
        "permissions": {"push": push, "pull": True},
    }


def _pr_payload(number: int = 7, *, state: str = "open", merged: bool = False) -> dict[str, Any]:
    return {
        "number": number,
        "html_url": f"https://github.com/acme/widgets/pull/{number}",
        "state": state,
        "draft": True,
        "merged_at": "2026-11-14T00:00:00Z" if merged else None,
        "head": {"sha": "a" * 40, "ref": "gaia/abc"},
        "base": {"sha": "b" * 40, "ref": "main"},
    }


# ------------------------------------------------------------------ headers --
async def test_client_sends_the_documented_headers_and_bearer_token() -> None:
    fake = GitHubFake()
    fake.add_json("GET", "/repos/acme/widgets", _repo_payload())

    async with _client(fake) as client:
        await client.get_repo(SLUG)

    request = fake.calls[0]
    assert request.headers["Accept"] == "application/vnd.github+json"
    assert request.headers["X-GitHub-Api-Version"] == "2022-11-28"
    assert request.headers["User-Agent"] == "gaia"
    assert request.headers["Authorization"] == f"Bearer {TOKEN}"


# ------------------------------------------------------------------- errors --
@pytest.mark.parametrize(
    ("status", "error_type"),
    [(401, GitHubAuthError), (403, GitHubPermissionError), (404, GitHubNotFound)],
)
async def test_error_mapping_for_get_repo(status: int, error_type: type[GitHubError]) -> None:
    fake = GitHubFake()
    fake.add_json(
        "GET",
        "/repos/acme/widgets",
        {"message": "nope", "documentation_url": "https://docs.github.com/rest"},
        status_code=status,
    )

    with pytest.raises(error_type) as caught:
        async with _client(fake) as client:
            await client.get_repo(SLUG)

    assert caught.value.status == status
    assert "nope" in str(caught.value)


async def test_error_message_never_contains_the_token() -> None:
    fake = GitHubFake()
    fake.add_json(
        "GET",
        "/repos/acme/widgets",
        {"message": f"token {TOKEN} was rejected"},
        status_code=401,
    )

    with pytest.raises(GitHubAuthError) as caught:
        async with _client(fake) as client:
            await client.get_repo(SLUG)

    assert TOKEN not in str(caught.value)
    assert TOKEN not in repr(caught.value)
    assert "[REDACTED]" in str(caught.value)


async def test_merge_error_mapping_405_409_422() -> None:
    fake = GitHubFake()
    fake.add_json(
        "PUT",
        "/repos/acme/widgets/pulls/7/merge",
        {"message": "Pull Request is not mergeable"},
        status_code=405,
    )

    with pytest.raises(MergeBlocked) as caught:
        async with _client(fake) as client:
            await client.merge_pr(SLUG, 7, sha="a" * 40, commit_title="merge")
    assert caught.value.status == 405
    assert "not mergeable" in str(caught.value)

    fake = GitHubFake()
    fake.add_json(
        "PUT",
        "/repos/acme/widgets/pulls/7/merge",
        {"message": "head changed"},
        status_code=409,
    )
    with pytest.raises(MergeBlocked, match="head changed since it was approved"):
        async with _client(fake) as client:
            await client.merge_pr(SLUG, 7, sha="a" * 40, commit_title="merge")

    fake = GitHubFake()
    fake.add_json(
        "PUT",
        "/repos/acme/widgets/pulls/7/merge",
        {"message": "Pull Request is not mergeable"},
        status_code=422,
    )
    with pytest.raises(MergeBlocked, match="not mergeable"):
        async with _client(fake) as client:
            await client.merge_pr(SLUG, 7, sha="a" * 40, commit_title="merge")


async def test_get_retries_a_500_twice_then_raises() -> None:
    fake = GitHubFake()
    fake.add_json(
        "GET",
        "/repos/acme/widgets",
        {"message": "server error"},
        status_code=500,
    )
    sleeps: list[float] = []

    with pytest.raises(GitHubError) as caught:
        async with _client(fake, sleep=sleeps.append) as client:
            await client.get_repo(SLUG)

    assert caught.value.status == 500
    assert sleeps == [1.0, 2.0]
    assert len(fake.calls) == 3


async def test_post_does_not_retry_a_500() -> None:
    fake = GitHubFake()
    fake.add_json(
        "POST",
        "/repos/acme/widgets/pulls",
        {"message": "server error"},
        status_code=500,
    )
    sleeps: list[float] = []

    with pytest.raises(GitHubError) as caught:
        async with _client(fake, sleep=sleeps.append) as client:
            await client.create_pr(
                SLUG,
                head="gaia/abc",
                base="main",
                title="[gaia] test",
                body="body",
                draft=True,
            )

    assert caught.value.status == 500
    assert sleeps == []
    assert len(fake.calls) == 1


# -------------------------------------------------------------- rate limits --
async def test_429_with_retry_after_sleeps_then_retries() -> None:
    responses = [
        json_response(
            429,
            {"message": "rate limited"},
            headers={"retry-after": "0.25"},
        ),
        json_response(200, _repo_payload()),
    ]
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        response = responses[calls]
        calls += 1
        return response

    sleeps: list[float] = []
    async with _client_with_handler(handler, sleep=sleeps.append, max_wait_s=60.0) as client:
        await client.get_repo(SLUG)

    assert calls == 2
    assert sleeps == [0.25]


async def test_403_remaining_zero_uses_reset_through_the_injected_clock() -> None:
    responses = [
        json_response(
            403,
            {"message": "rate limited"},
            headers={"x-ratelimit-remaining": "0", "x-ratelimit-reset": "1010.5"},
        ),
        json_response(200, _repo_payload()),
    ]
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        response = responses[calls]
        calls += 1
        return response

    sleeps: list[float] = []
    now = 1000.0
    async with _client_with_handler(
        handler, sleep=sleeps.append, now=lambda: now, max_wait_s=60.0
    ) as client:
        await client.get_repo(SLUG)

    assert calls == 2
    assert sleeps == [10.5]


async def test_rate_limit_over_max_wait_raises_without_sleeping() -> None:
    fake = GitHubFake()
    fake.add_json(
        "GET",
        "/repos/acme/widgets",
        {"message": "rate limited"},
        status_code=429,
        headers={"retry-after": "120"},
    )
    sleeps: list[float] = []

    with pytest.raises(RateLimited) as caught:
        async with _client(fake, sleep=sleeps.append, max_wait_s=60.0) as client:
            await client.get_repo(SLUG)

    assert caught.value.retry_after_s == 120.0
    assert sleeps == []


# --------------------------------------------------------------- check_token --
async def test_check_token_all_present() -> None:
    fake = GitHubFake()
    fake.add_json("GET", "/repos/acme/widgets", _repo_payload())
    fake.add_json("GET", "/repos/acme/widgets/pulls", [])
    fake.add_json("GET", "/repos/acme/widgets/issues", [])
    fake.add_json("GET", "/repos/acme/widgets/commits/main/status", {})
    fake.add_json(
        "GET",
        "/repos/acme/widgets/commits/main/check-runs",
        {"check_runs": []},
    )

    async with _client(fake) as client:
        result = await check_token(client, SLUG)

    assert result == TokenCheck(ok=True, missing=())


async def test_check_token_missing_metadata() -> None:
    fake = GitHubFake()
    fake.add_json(
        "GET",
        "/repos/acme/widgets",
        {"message": "Not Found"},
        status_code=404,
    )

    async with _client(fake) as client:
        result = await check_token(client, SLUG)

    assert result == TokenCheck(ok=False, missing=("Metadata R",))
    assert len(fake.calls) == 1


async def test_check_token_missing_contents() -> None:
    fake = GitHubFake()
    fake.add_json("GET", "/repos/acme/widgets", _repo_payload(push=False))
    fake.add_json("GET", "/repos/acme/widgets/pulls", [])
    fake.add_json("GET", "/repos/acme/widgets/issues", [])
    fake.add_json("GET", "/repos/acme/widgets/commits/main/status", {})
    fake.add_json("GET", "/repos/acme/widgets/commits/main/check-runs", {})

    async with _client(fake) as client:
        result = await check_token(client, SLUG)

    assert result == TokenCheck(ok=False, missing=("Contents R/W",))


@pytest.mark.parametrize(
    ("path", "permission"),
    [
        ("/repos/acme/widgets/pulls", "Pull requests R"),
        ("/repos/acme/widgets/issues", "Issues R"),
        ("/repos/acme/widgets/commits/main/status", "Commit statuses R"),
        ("/repos/acme/widgets/commits/main/check-runs", "Checks R"),
    ],
)
async def test_check_token_missing_one_read_permission(path: str, permission: str) -> None:
    fake = GitHubFake()
    fake.add_json("GET", "/repos/acme/widgets", _repo_payload())
    for candidate in (
        "/repos/acme/widgets/pulls",
        "/repos/acme/widgets/issues",
        "/repos/acme/widgets/commits/main/status",
        "/repos/acme/widgets/commits/main/check-runs",
    ):
        if candidate == path:
            fake.add_json("GET", candidate, {"message": "Forbidden"}, status_code=403)
        else:
            fake.add_json("GET", candidate, [])

    async with _client(fake) as client:
        result = await check_token(client, SLUG)

    assert result == TokenCheck(ok=False, missing=(permission,))


async def test_delete_branch_refuses_non_gaia_branch() -> None:
    fake = GitHubFake()

    with pytest.raises(ValueError, match="non-gaia"):
        async with _client(fake) as client:
            await client.delete_branch(SLUG, "main")


async def test_delete_branch_treats_404_and_422_as_already_gone() -> None:
    fake = GitHubFake()
    fake.add_json(
        "DELETE",
        "/repos/acme/widgets/git/refs/heads/gaia/abc",
        {"message": "not found"},
        status_code=404,
    )

    async with _client(fake) as client:
        await client.delete_branch(SLUG, "gaia/abc")

    fake = GitHubFake()
    fake.add_json(
        "DELETE",
        "/repos/acme/widgets/git/refs/heads/gaia/abc",
        {"message": "reference does not exist"},
        status_code=422,
    )
    async with _client(fake) as client:
        await client.delete_branch(SLUG, "gaia/abc")


async def test_delete_branch_raises_on_other_errors() -> None:
    fake = GitHubFake()
    fake.add_json(
        "DELETE",
        "/repos/acme/widgets/git/refs/heads/gaia/abc",
        {"message": "Forbidden"},
        status_code=403,
    )

    with pytest.raises(GitHubPermissionError):
        async with _client(fake) as client:
            await client.delete_branch(SLUG, "gaia/abc")


# ------------------------------------------------------- unparseable bodies --
async def test_get_repo_raises_when_success_body_is_not_json() -> None:
    fake = GitHubFake()
    fake.add("GET", "/repos/acme/widgets", httpx.Response(200, text="not json"))

    with pytest.raises(GitHubError, match="unparseable response body"):
        async with _client(fake) as client:
            await client.get_repo(SLUG)


@pytest.mark.parametrize(
    ("payload", "match"),
    [
        ([], "unparseable repo response"),
        ({"default_branch": None, "private": True}, "without a default branch"),
        ({"default_branch": "main", "private": "yes"}, "without a private flag"),
    ],
)
async def test_get_repo_raises_on_malformed_payload(payload: object, match: str) -> None:
    fake = GitHubFake()
    fake.add_json("GET", "/repos/acme/widgets", payload)

    with pytest.raises(GitHubError, match=match):
        async with _client(fake) as client:
            await client.get_repo(SLUG)


async def test_get_repo_defaults_missing_permissions_to_empty() -> None:
    fake = GitHubFake()
    fake.add_json(
        "GET",
        "/repos/acme/widgets",
        {"default_branch": "main", "private": False, "permissions": []},
    )

    async with _client(fake) as client:
        repo = await client.get_repo(SLUG)

    assert repo.permissions == {}


async def test_error_message_builds_from_message_and_error_details() -> None:
    fake = GitHubFake()
    fake.add_json(
        "GET",
        "/repos/acme/widgets",
        {
            "message": "Validation Failed",
            "errors": [{"message": "head is required"}, {"code": "missing_field"}],
        },
        status_code=422,
    )

    with pytest.raises(GitHubError) as caught:
        async with _client(fake) as client:
            await client.get_repo(SLUG)

    assert caught.value.status == 422
    assert "Validation Failed" in str(caught.value)
    assert "head is required" in str(caught.value)
    assert "missing_field" in str(caught.value)


async def test_error_body_that_is_not_json_has_a_clear_fallback() -> None:
    fake = GitHubFake()
    fake.add("GET", "/repos/acme/widgets", httpx.Response(401, text="plain text"))

    with pytest.raises(GitHubAuthError, match="GitHub API error"):
        async with _client(fake) as client:
            await client.get_repo(SLUG)


async def test_error_message_skips_non_dict_error_details() -> None:
    fake = GitHubFake()
    fake.add_json(
        "GET",
        "/repos/acme/widgets",
        {"message": "Validation Failed", "errors": ["bad error detail"]},
        status_code=422,
    )

    with pytest.raises(GitHubError, match="Validation Failed"):
        async with _client(fake) as client:
            await client.get_repo(SLUG)


async def test_error_message_uses_a_blank_string_message_verbatim() -> None:
    fake = GitHubFake()
    fake.add_json(
        "GET",
        "/repos/acme/widgets",
        {"message": "   "},
        status_code=401,
    )

    with pytest.raises(GitHubAuthError):
        async with _client(fake) as client:
            await client.get_repo(SLUG)


async def test_429_with_an_invalid_retry_after_uses_the_default_wait() -> None:
    responses = [
        json_response(
            429,
            {"message": "rate limited"},
            headers={"retry-after": "tomorrow"},
        ),
        json_response(200, _repo_payload()),
    ]
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        response = responses[calls]
        calls += 1
        return response

    sleeps: list[float] = []
    async with _client_with_handler(handler, sleep=sleeps.append, max_wait_s=61.0) as client:
        await client.get_repo(SLUG)

    assert calls == 2
    assert sleeps == [60.0]


async def test_429_with_an_invalid_reset_uses_the_default_wait() -> None:
    responses = [
        json_response(
            429,
            {"message": "rate limited"},
            headers={"x-ratelimit-reset": "later"},
        ),
        json_response(200, _repo_payload()),
    ]
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        response = responses[calls]
        calls += 1
        return response

    sleeps: list[float] = []
    async with _client_with_handler(handler, sleep=sleeps.append, max_wait_s=61.0) as client:
        await client.get_repo(SLUG)

    assert calls == 2
    assert sleeps == [60.0]


# ------------------------------------------------------- PR parse failures --
async def test_find_open_pr_raises_on_a_non_list_body() -> None:
    fake = GitHubFake()
    fake.add_json("GET", "/repos/acme/widgets/pulls", {"not": "a list"})

    with pytest.raises(GitHubError, match="unparseable pulls response"):
        async with _client(fake) as client:
            await client.find_open_pr(SLUG, "gaia/abc")


async def test_find_open_pr_raises_on_a_non_dict_item() -> None:
    fake = GitHubFake()
    fake.add_json("GET", "/repos/acme/widgets/pulls", ["not a dict"])

    with pytest.raises(GitHubError, match="unparseable pull request"):
        async with _client(fake) as client:
            await client.find_open_pr(SLUG, "gaia/abc")


async def test_create_pr_raises_on_a_non_dict_body() -> None:
    fake = GitHubFake()
    fake.add_json("POST", "/repos/acme/widgets/pulls", [])

    with pytest.raises(GitHubError, match="unparseable pull request"):
        async with _client(fake) as client:
            await client.create_pr(
                SLUG, head="gaia/abc", base="main", title="t", body="b", draft=True
            )


async def test_get_pr_raises_on_a_non_dict_body() -> None:
    fake = GitHubFake()
    fake.add_json("GET", "/repos/acme/widgets/pulls/9", [])

    with pytest.raises(GitHubError, match="unparseable pull request"):
        async with _client(fake) as client:
            await client.get_pr(SLUG, 9)


@pytest.mark.parametrize(
    ("payload", "match"),
    [
        ({}, "without a number"),
        ({"number": 9}, "without an html_url"),
        ({"number": 9, "html_url": "u", "draft": "yes"}, "without a draft flag"),
        ({"number": 9, "html_url": "u", "draft": True}, "without head/base refs"),
        (
            {"number": 9, "html_url": "u", "draft": True, "head": {}, "base": {"ref": "main"}},
            "without a head sha",
        ),
        (
            {
                "number": 9,
                "html_url": "u",
                "draft": True,
                "head": {"sha": "a" * 40},
                "base": {"ref": "main"},
            },
            "without head/base ref names",
        ),
    ],
)
async def test_get_pr_raises_on_malformed_pr_payload(
    payload: dict[str, object], match: str
) -> None:
    fake = GitHubFake()
    fake.add_json("GET", "/repos/acme/widgets/pulls/9", payload)

    with pytest.raises(GitHubError, match=match):
        async with _client(fake) as client:
            await client.get_pr(SLUG, 9)


async def test_get_pr_raises_on_an_unknown_state() -> None:
    fake = GitHubFake()
    fake.add_json(
        "GET",
        "/repos/acme/widgets/pulls/9",
        _pr_payload(number=9, state="weird"),
    )

    with pytest.raises(GitHubError, match="unknown pull request state"):
        async with _client(fake) as client:
            await client.get_pr(SLUG, 9)


# --------------------------------------------------------- merge parse errors --
async def test_merge_pr_raises_on_a_non_specific_error() -> None:
    fake = GitHubFake()
    fake.add_json(
        "PUT",
        "/repos/acme/widgets/pulls/7/merge",
        {"message": "Forbidden"},
        status_code=403,
    )

    with pytest.raises(GitHubPermissionError):
        async with _client(fake) as client:
            await client.merge_pr(SLUG, 7, sha="a" * 40, commit_title="merge")


@pytest.mark.parametrize(
    ("payload", "match"),
    [
        ([], "unparseable merge response"),
        ({"merged": True}, "merge without a sha"),
        ({"sha": "a" * 40}, "merge without a merged flag"),
    ],
)
async def test_merge_pr_raises_on_malformed_success(payload: object, match: str) -> None:
    fake = GitHubFake()
    fake.add_json("PUT", "/repos/acme/widgets/pulls/7/merge", payload)

    with pytest.raises(GitHubError, match=match):
        async with _client(fake) as client:
            await client.merge_pr(SLUG, 7, sha="a" * 40, commit_title="merge")


def _pr(*, draft: bool, node_id: str = "PR_node_1") -> PullRequestInfo:
    return PullRequestInfo(
        number=1,
        url="https://github.com/acme/widgets/pull/1",
        state="open",
        draft=draft,
        head_sha="a" * 40,
        head_ref="gaia/abc",
        base_ref="main",
        node_id=node_id,
    )


async def test_mark_ready_skips_a_pr_that_is_not_a_draft() -> None:
    fake = GitHubFake()
    async with _client(fake) as client:
        await client.mark_ready_for_review(_pr(draft=False))
    assert fake.calls == []


async def test_mark_ready_sends_the_graphql_mutation() -> None:
    fake = GitHubFake()
    fake.add_json("POST", "/graphql", {"data": {"markPullRequestReadyForReview": {}}})
    async with _client(fake) as client:
        await client.mark_ready_for_review(_pr(draft=True))
    body = json.loads(fake.calls[0].content)
    assert "markPullRequestReadyForReview" in body["query"]
    assert body["variables"] == {"id": "PR_node_1"}


async def test_mark_ready_refuses_without_a_node_id() -> None:
    async with _client(GitHubFake()) as client:
        with pytest.raises(GitHubError, match="no node_id"):
            await client.mark_ready_for_review(_pr(draft=True, node_id=""))


async def test_mark_ready_raises_graphql_errors_and_http_errors() -> None:
    fake = GitHubFake()
    fake.add_json("POST", "/graphql", {"errors": [{"message": "Resource not accessible"}]})
    async with _client(fake) as client:
        with pytest.raises(GitHubError, match="Resource not accessible"):
            await client.mark_ready_for_review(_pr(draft=True))

    unauthorised = GitHubFake()
    unauthorised.add_json("POST", "/graphql", {"message": "Bad credentials"}, status_code=401)
    async with _client(unauthorised) as client:
        with pytest.raises(GitHubAuthError):
            await client.mark_ready_for_review(_pr(draft=True))


async def test_delete_branch_keeps_the_slash_literal_on_the_wire() -> None:
    """Lead review G1: ``%2F`` 404s on GitHub, which delete_branch reads as gone."""
    fake = GitHubFake()
    fake.add("DELETE", "/repos/acme/widgets/git/refs/heads/gaia/abc", httpx.Response(204))
    async with _client(fake) as client:
        await client.delete_branch(SLUG, "gaia/abc")
    assert b"%2F" not in fake.calls[0].url.raw_path
