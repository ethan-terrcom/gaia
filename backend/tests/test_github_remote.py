"""Doc 15a remote parsing: accepted GitHub remote forms and refused strangers."""

from __future__ import annotations

import pytest

from gaia.services.github import RepoSlug, UnsupportedRemote, parse_github_remote


@pytest.mark.parametrize(
    ("url", "owner", "name"),
    [
        ("https://github.com/acme/widgets", "acme", "widgets"),
        ("https://github.com/acme/widgets.git", "acme", "widgets"),
        ("https://acme:github_pat_planted@github.com/acme/widgets.git", "acme", "widgets"),
        ("git@github.com:acme/widgets.git", "acme", "widgets"),
        ("ssh://git@github.com/acme/widgets.git", "acme", "widgets"),
    ],
)
def test_parse_github_remote_accepts_the_documented_forms(url: str, owner: str, name: str) -> None:
    slug = parse_github_remote(url)

    assert slug == RepoSlug(owner=owner, name=name)
    assert slug.full_name == f"{owner}/{name}"
    # Gaia always pushes over HTTPS, whatever the remote's original scheme was.
    assert slug.https_url == f"https://github.com/{owner}/{name}.git"


def test_parse_github_remote_accepts_owner_and_repo_dots_and_hyphens() -> None:
    assert parse_github_remote("https://github.com/acme-inc/widgets.js.git") == RepoSlug(
        owner="acme-inc", name="widgets.js"
    )


@pytest.mark.parametrize(
    "url",
    [
        "https://gitlab.com/acme/widgets.git",
        "ssh://git@gitlab.com/acme/widgets.git",
        "git@gitlab.com:acme/widgets.git",
        "https://github.com.acme.invalid/acme/widgets.git",
        "https://github.com/acme",
        "https://github.com/acme/widgets/extra",
        "https://github.com/acme/widgets.git?token=github_pat_planted",
        "https://github.com/acme/widgets.git#fragment",
        "ssh://git@github.com/acme/widgets",
        "git@github.com:acme/widgets",
        "",
        "not a url",
    ],
)
def test_parse_github_remote_rejects_other_hosts_and_malformed_urls(url: str) -> None:
    with pytest.raises(UnsupportedRemote):
        parse_github_remote(url)


@pytest.mark.parametrize(
    ("url", "fragment"),
    [
        ("https://github.com//widgets.git", "owner/repository remote"),
        ("https://github.com/./widgets.git", "."),
        ("https://github.com/acme/..", ".."),
        ("https://github.com/bad owner/widgets.git", "bad owner"),
        ("https://github.com/acme/bad/repo.git", "bad"),
        ("https://github.com/acme/bad%20name.git", "bad%20name"),
    ],
)
def test_parse_github_remote_rejects_invalid_owner_or_name(url: str, fragment: str) -> None:
    with pytest.raises(UnsupportedRemote, match=fragment):
        parse_github_remote(url)
