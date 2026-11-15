"""Doc 15b pure rollups: ci_rollup and review_rollup."""

from __future__ import annotations

import pytest

from gaia.domain.enums import CiStatus
from gaia.services.ci import ci_rollup, review_rollup
from gaia.services.github import CheckRun, CombinedStatus, CommitStatus, Review


def _status(
    *,
    state: str = "pending",
    total_count: int = 0,
    statuses: tuple[CommitStatus, ...] = (),
) -> CombinedStatus:
    return CombinedStatus(state=state, total_count=total_count, statuses=statuses)


def _check(
    *, name: str = "ci", status: str = "completed", conclusion: str | None = "success"
) -> CheckRun:
    return CheckRun(name=name, status=status, conclusion=conclusion)


@pytest.mark.parametrize(
    ("status", "checks", "expected"),
    [
        # No statuses and no checks: the repository has no CI.
        (_status(), (), CiStatus.UNKNOWN),
        # GitHub's "no statuses at all" marker is still no CI.
        (_status(state="pending", total_count=0), (), CiStatus.UNKNOWN),
        # One passing commit status is enough to pass.
        (
            _status(
                state="success",
                total_count=1,
                statuses=(CommitStatus("ci/test", "success"),),
            ),
            (),
            CiStatus.PASSED,
        ),
        # A pending individual commit status keeps the PR pending.
        (
            _status(
                state="pending",
                total_count=1,
                statuses=(CommitStatus("ci/test", "pending"),),
            ),
            (),
            CiStatus.PENDING,
        ),
        # A top-level pending with statuses keeps the PR pending.
        (
            _status(state="pending", total_count=1, statuses=(CommitStatus("ci/test", "success"),)),
            (),
            CiStatus.PENDING,
        ),
        # Top-level failure is failed.
        (_status(state="failure", total_count=0), (), CiStatus.FAILED),
        (
            _status(
                state="error",
                total_count=1,
                statuses=(CommitStatus("ci/test", "success"),),
            ),
            (),
            CiStatus.FAILED,
        ),
        # Individual failure/error statuses fail even when the top-level says success.
        (
            _status(
                state="success",
                total_count=2,
                statuses=(CommitStatus("ci/one", "success"), CommitStatus("ci/two", "failure")),
            ),
            (),
            CiStatus.FAILED,
        ),
        (
            _status(
                state="success",
                total_count=1,
                statuses=(CommitStatus("ci/one", "error"),),
            ),
            (),
            CiStatus.FAILED,
        ),
        # A check that is not completed keeps the PR pending.
        (
            _status(state="success", total_count=0),
            (_check(status="in_progress"),),
            CiStatus.PENDING,
        ),
        # Completed passing conclusions count as passing.
        (
            _status(),
            (_check(conclusion="neutral"), _check(conclusion="skipped")),
            CiStatus.PASSED,
        ),
        # Completed non-failing conclusion with no statuses still passes.
        (_status(state="pending", total_count=0), (_check(conclusion="success"),), CiStatus.PASSED),
        # A completed failing conclusion fails.
        (
            _status(),
            (_check(conclusion="failure"),),
            CiStatus.FAILED,
        ),
        (
            _status(),
            (_check(conclusion="cancelled"),),
            CiStatus.FAILED,
        ),
        (
            _status(),
            (_check(conclusion="timed_out"),),
            CiStatus.FAILED,
        ),
        (
            _status(),
            (_check(conclusion="action_required"),),
            CiStatus.FAILED,
        ),
        (
            _status(),
            (_check(conclusion="startup_failure"),),
            CiStatus.FAILED,
        ),
        (
            _status(),
            (_check(conclusion="stale"),),
            CiStatus.FAILED,
        ),
        # A failing conclusion on an unfinished check is not a failure yet.
        (
            _status(),
            (_check(status="in_progress", conclusion="failure"),),
            CiStatus.PENDING,
        ),
    ],
)
def test_ci_rollup_table(
    status: CombinedStatus,
    checks: tuple[CheckRun, ...],
    expected: CiStatus,
) -> None:
    assert ci_rollup(status, checks) is expected


def _review(
    *,
    user: str,
    state: str,
    submitted_at: str | None = "2026-11-14T00:00:00Z",
) -> Review:
    return Review(user_login=user, state=state, submitted_at=submitted_at)


def test_review_rollup_latest_per_user_wins() -> None:
    reviews = (
        _review(user="alice", state="APPROVED", submitted_at="2026-11-14T00:00:00Z"),
        _review(user="alice", state="CHANGES_REQUESTED", submitted_at="2026-11-14T00:01:00Z"),
    )

    assert review_rollup(reviews) == "changes_requested"


def test_review_rollup_comments_and_pending_do_not_change_a_user_state() -> None:
    reviews = (
        _review(user="alice", state="APPROVED", submitted_at="2026-11-14T00:00:00Z"),
        _review(user="alice", state="COMMENTED", submitted_at="2026-11-14T00:02:00Z"),
        _review(user="alice", state="PENDING", submitted_at="2026-11-14T00:03:00Z"),
    )

    assert review_rollup(reviews) == "approved"


def test_review_rollup_an_older_review_does_not_override_a_newer_one() -> None:
    reviews = (
        _review(user="alice", state="APPROVED", submitted_at="2026-11-14T00:02:00Z"),
        _review(user="alice", state="CHANGES_REQUESTED", submitted_at="2026-11-14T00:01:00Z"),
    )

    assert review_rollup(reviews) == "approved"


def test_review_rollup_a_dismissal_clears_an_approval() -> None:
    reviews = (
        _review(user="alice", state="APPROVED", submitted_at="2026-11-14T00:00:00Z"),
        _review(user="alice", state="DISMISSED", submitted_at="2026-11-14T00:01:00Z"),
    )

    assert review_rollup(reviews) == "none"


def test_review_rollup_any_changes_requested_beats_approvals() -> None:
    reviews = (
        _review(user="alice", state="APPROVED", submitted_at="2026-11-14T00:00:00Z"),
        _review(user="bob", state="CHANGES_REQUESTED", submitted_at="2026-11-14T00:01:00Z"),
    )

    assert review_rollup(reviews) == "changes_requested"


def test_review_rollup_ignores_reviews_without_submitted_at() -> None:
    reviews = (
        _review(user="alice", state="APPROVED", submitted_at=None),
        _review(user="bob", state="CHANGES_REQUESTED", submitted_at=None),
    )

    assert review_rollup(reviews) == "none"


def test_review_rollup_no_reviews_is_none() -> None:
    assert review_rollup(()) == "none"
