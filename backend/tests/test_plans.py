"""Structured-output models for lead planning and review (doc 11)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from gaia.domain.plans import Plan, Review, TicketInput


def _step(**overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "title": "Add a test",
        "instructions": "Write a test and run it.",
        "files_likely_touched": ["tests/test_x.py"],
        "acceptance": ["pytest passes"],
        "test_command": ["pytest", "-q"],
    }
    values.update(overrides)
    return values


def _plan(**overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "summary": "Add a test.",
        "risks": [],
        "steps": [_step()],
    }
    values.update(overrides)
    return values


def test_valid_plan_parses() -> None:
    plan = Plan.model_validate(_plan())

    assert plan.summary == "Add a test."
    assert len(plan.steps) == 1
    assert plan.steps[0].test_command == ["pytest", "-q"]


def test_ticket_input_is_frozen() -> None:
    ticket = TicketInput(id="t1", title="Ship", body="Do the thing")

    with pytest.raises(ValidationError):
        ticket.title = "changed"  # type: ignore[misc]


def test_plan_requires_between_1_and_10_steps() -> None:
    with pytest.raises(ValidationError, match="steps must contain"):
        Plan.model_validate(_plan(steps=[]))

    with pytest.raises(ValidationError, match="steps must contain"):
        Plan.model_validate(_plan(steps=[_step() for _ in range(11)]))


def test_step_title_is_at_most_200_characters() -> None:
    Plan.model_validate(_plan(steps=[_step(title="x" * 200)]))

    with pytest.raises(ValidationError, match="title must be at most 200"):
        Plan.model_validate(_plan(steps=[_step(title="x" * 201)]))


def test_step_instructions_are_between_1_and_20000_characters() -> None:
    with pytest.raises(ValidationError, match="instructions must be between"):
        Plan.model_validate(_plan(steps=[_step(instructions="")]))

    Plan.model_validate(_plan(steps=[_step(instructions="x" * 20_000)]))

    with pytest.raises(ValidationError, match="instructions must be between"):
        Plan.model_validate(_plan(steps=[_step(instructions="x" * 20_001)]))


def test_test_command_is_none_or_a_sane_argv_list() -> None:
    Plan.model_validate(_plan(steps=[_step(test_command=None)]))

    with pytest.raises(ValidationError, match="test_command must be None or"):
        Plan.model_validate(_plan(steps=[_step(test_command=[])]))

    with pytest.raises(ValidationError, match="test_command must be None or"):
        Plan.model_validate(_plan(steps=[_step(test_command=["x"] * 65)]))

    with pytest.raises(ValidationError, match="each test_command item"):
        Plan.model_validate(_plan(steps=[_step(test_command=[""])]))

    with pytest.raises(ValidationError, match="each test_command item"):
        Plan.model_validate(_plan(steps=[_step(test_command=["x" * 1001])]))

    with pytest.raises(ValidationError, match="NUL"):
        Plan.model_validate(_plan(steps=[_step(test_command=["pytest\x00"])]))


def test_review_verdict_round_trip() -> None:
    review = Review.model_validate({"verdict": "revise", "feedback": "fix it", "issues": []})
    assert review.verdict == "revise"

    with pytest.raises(ValidationError):
        Review.model_validate({"verdict": "maybe", "feedback": "", "issues": []})
