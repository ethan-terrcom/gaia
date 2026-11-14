"""Structured-output models for lead planning and review (doc 11).

These models are deliberately plain: the Anthropic SDK turns them into a JSON
schema for structured outputs, and the API accepts only a small subset of JSON
Schema. No custom ``Field`` regexes, discriminated unions or recursive types
live here; the validators below run after the SDK has parsed the model's JSON.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, field_validator

MAX_PLAN_STEPS = 10
MAX_STEP_TITLE_CHARS = 200
MAX_STEP_INSTRUCTIONS_CHARS = 20_000
MAX_TEST_COMMAND_ITEMS = 64
MAX_TEST_COMMAND_ITEM_CHARS = 1000


class TicketInput(BaseModel):
    """The ticket fields the lead needs, with no repository row attached."""

    model_config = ConfigDict(frozen=True)

    id: str
    title: str
    body: str


class PlanStep(BaseModel):
    """One independently testable unit of work for a junior engineer."""

    title: str
    instructions: str
    files_likely_touched: list[str]
    acceptance: list[str]
    test_command: list[str] | None = None

    @field_validator("title")
    @classmethod
    def _title_is_short_enough(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("title must not be empty")
        if len(value) > MAX_STEP_TITLE_CHARS:
            raise ValueError(
                f"title must be at most {MAX_STEP_TITLE_CHARS} characters, got {len(value)}"
            )
        return value

    @field_validator("instructions")
    @classmethod
    def _instructions_are_1_to_20_000_chars(cls, value: str) -> str:
        if not 1 <= len(value) <= MAX_STEP_INSTRUCTIONS_CHARS:
            raise ValueError(
                "instructions must be between 1 and "
                f"{MAX_STEP_INSTRUCTIONS_CHARS} characters, got {len(value)}"
            )
        return value

    @field_validator("test_command")
    @classmethod
    def _test_command_is_a_sane_argv_list(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return value
        if not 1 <= len(value) <= MAX_TEST_COMMAND_ITEMS:
            raise ValueError(
                "test_command must be None or have between 1 and "
                f"{MAX_TEST_COMMAND_ITEMS} items, got {len(value)}"
            )
        for item in value:
            if not 1 <= len(item) <= MAX_TEST_COMMAND_ITEM_CHARS:
                raise ValueError(
                    "each test_command item must be between 1 and "
                    f"{MAX_TEST_COMMAND_ITEM_CHARS} characters"
                )
            if "\x00" in item:
                raise ValueError("test_command items must not contain NUL characters")
        return value


class Plan(BaseModel):
    """A lead plan with 1 to 10 steps."""

    summary: str
    risks: list[str]
    steps: list[PlanStep]

    @field_validator("steps")
    @classmethod
    def _steps_are_1_to_10(cls, value: list[PlanStep]) -> list[PlanStep]:
        if not 1 <= len(value) <= MAX_PLAN_STEPS:
            raise ValueError(
                f"steps must contain between 1 and {MAX_PLAN_STEPS} items, got {len(value)}"
            )
        return value


class Review(BaseModel):
    """The lead's verdict on one step or the final diff."""

    verdict: Literal["accept", "revise", "escalate"]
    feedback: str
    issues: list[str]
