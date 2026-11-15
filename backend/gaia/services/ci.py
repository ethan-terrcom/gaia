"""Pure CI and review rollups for doc 15b.

The two functions here are deliberately free of network and database code so the
decision table can be unit-tested exhaustively. They take the frozen dataclasses
returned by :class:`gaia.services.github.GitHubClient` and return the strings the
rest of Gaia stores.
"""

from __future__ import annotations

from collections.abc import Sequence

from gaia.domain.enums import CiStatus
from gaia.services.github import CheckRun, CombinedStatus, Review

#: Completed check-run conclusions that make a PR fail CI (doc 15b).
_FAILED_CHECK_CONCLUSIONS = frozenset(
    {"failure", "cancelled", "timed_out", "action_required", "startup_failure", "stale"}
)

#: Review states that can be a user's latest decision (doc 15b).
_DECISIVE_REVIEW_STATES = frozenset({"APPROVED", "CHANGES_REQUESTED", "DISMISSED"})


def ci_rollup(status: CombinedStatus, checks: Sequence[CheckRun]) -> CiStatus:
    """Roll combined commit statuses and check runs into one :class:`CiStatus`.

    The order follows doc 15b:

    - any failed status or failed check conclusion makes the PR **failed**;
    - otherwise any unfinished check, or a pending combined status that has
      statuses, makes it **pending**;
    - otherwise at least one status or check means **passed** (``success``,
      ``neutral`` and ``skipped`` conclusions count as passing);
    - with no statuses and no checks the repository has no CI: **unknown**.
    """
    status_failed = status.state in {"failure", "error"} or any(
        item.state in {"failure", "error"} for item in status.statuses
    )
    check_failed = any(
        check.status == "completed" and check.conclusion in _FAILED_CHECK_CONCLUSIONS
        for check in checks
    )
    if status_failed or check_failed:
        return CiStatus.FAILED

    check_pending = any(check.status != "completed" for check in checks)
    status_pending = status.total_count > 0 and (
        status.state == "pending" or any(item.state == "pending" for item in status.statuses)
    )
    if check_pending or status_pending:
        return CiStatus.PENDING

    if status.total_count > 0 or checks:
        return CiStatus.PASSED
    return CiStatus.UNKNOWN


def review_rollup(reviews: Sequence[Review]) -> str:
    """Return ``changes_requested``, ``approved`` or ``none`` for a PR's reviews.

    For each user, only their latest ``APPROVED``, ``CHANGES_REQUESTED`` or
    ``DISMISSED`` review counts. ``COMMENTED`` and ``PENDING`` reviews never
    change a user's state, and a review without ``submitted_at`` is ignored.
    """
    latest: dict[str, tuple[str, str]] = {}
    for review in reviews:
        submitted_at = review.submitted_at
        if submitted_at is None or review.state not in _DECISIVE_REVIEW_STATES:
            continue
        current = latest.get(review.user_login)
        if current is None or submitted_at > current[0]:
            latest[review.user_login] = (submitted_at, review.state)

    states = {state for _, state in latest.values()}
    if "CHANGES_REQUESTED" in states:
        return "changes_requested"
    if "APPROVED" in states:
        return "approved"
    return "none"
