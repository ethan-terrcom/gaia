"""The shared ``finish`` arguments (docs 10b, 10c)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from gaia.runtimes.finish import FINISH_TOOL_NAME, FinishArgs, finish_input_schema


def test_finish_schema_lists_required_fields() -> None:
    schema = finish_input_schema()
    assert FINISH_TOOL_NAME == "finish"
    assert set(schema["properties"]) == {"summary", "tests_run", "tests_passed", "concerns"}
    assert "summary" in schema["required"]
    assert "tests_passed" in schema["required"]
    assert schema["additionalProperties"] is False


def test_finish_rejects_extra_fields_and_empty_summary() -> None:
    with pytest.raises(ValidationError):
        FinishArgs.model_validate({"summary": "x", "tests_passed": True, "files_changed": ["a"]})
    with pytest.raises(ValidationError):
        FinishArgs.model_validate({"summary": "", "tests_passed": True})


def test_finish_defaults() -> None:
    args = FinishArgs.model_validate({"summary": "did it", "tests_passed": False})
    assert args.tests_run == []
    assert args.concerns == []
