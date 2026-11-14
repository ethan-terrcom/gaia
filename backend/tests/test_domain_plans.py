"""Validators for the doc 11 plan models used by the 12a skeleton."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from gaia.domain.plans import Plan, PlanStep


def make_step(**overrides: Any) -> dict[str, Any]:
    """Return a valid PlanStep payload, with optional overrides."""
    step: dict[str, Any] = {
        "title": "Do the thing",
        "instructions": "Do it carefully",
        "files_likely_touched": ["file.py"],
        "acceptance": ["It works"],
    }
    step.update(overrides)
    return step


def test_plan_step_title_has_a_length_limit() -> None:
    with pytest.raises(ValidationError, match="title"):
        PlanStep.model_validate(make_step(title=""))
    with pytest.raises(ValidationError, match="title"):
        PlanStep.model_validate(make_step(title="x" * 201))


def test_plan_step_instructions_have_a_length_limit() -> None:
    with pytest.raises(ValidationError, match="instructions"):
        PlanStep.model_validate(make_step(instructions=""))
    with pytest.raises(ValidationError, match="instructions"):
        PlanStep.model_validate(make_step(instructions="x" * 20_001))


def test_plan_step_test_command_can_be_explicitly_none() -> None:
    step = PlanStep.model_validate(make_step(test_command=None))
    assert step.test_command is None


def test_plan_step_test_command_has_argv_limits() -> None:
    with pytest.raises(ValidationError, match="between 1 and 64"):
        PlanStep.model_validate(make_step(test_command=[]))

    with pytest.raises(ValidationError, match="between 1 and 1000"):
        PlanStep.model_validate(make_step(test_command=[""]))

    with pytest.raises(ValidationError, match="between 1 and 1000"):
        PlanStep.model_validate(make_step(test_command=["x" * 1001]))

    with pytest.raises(ValidationError, match="NUL"):
        PlanStep.model_validate(make_step(test_command=["bad\x00argv"]))


def test_a_plan_has_between_one_and_ten_steps() -> None:
    valid_step = make_step()
    with pytest.raises(ValidationError, match="between 1 and 10"):
        Plan.model_validate({"summary": "A plan", "risks": [], "steps": []})
    with pytest.raises(ValidationError, match="between 1 and 10"):
        Plan.model_validate({"summary": "A plan", "risks": [], "steps": [valid_step] * 11})
