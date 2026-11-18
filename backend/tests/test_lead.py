"""Lead planning and review (doc 11)."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import pytest

from gaia.domain.plans import Plan, PlanStep, Review, TicketInput
from gaia.llm.base import LLMStop, LLMUsage, ParsedResponse
from gaia.llm.fake import FakeLLM
from gaia.orchestrator import lead
from gaia.orchestrator.deps import CheckOutcome
from gaia.orchestrator.lead import (
    EARLIER_CHECK_OUTPUT_CAP,
    LeadEngineer,
    LeadEscalation,
    PlanRejected,
    truncate_diff,
)
from gaia.orchestrator.repo_context import RepoContext
from gaia.security.commands import CommandPolicy


def _usage() -> LLMUsage:
    return LLMUsage(provider="fake", model="fake-model", input_tokens=10, output_tokens=5)


def _plan(test_command: list[str] | None = None) -> Plan:
    return Plan(
        summary="Add a test",
        risks=[],
        steps=[
            PlanStep(
                title="Write a test",
                instructions="Add a failing test and make it pass.",
                files_likely_touched=["tests/test_x.py"],
                acceptance=["pytest passes"],
                test_command=test_command,
            )
        ],
    )


def _parsed_plan(plan: Plan) -> ParsedResponse[Plan]:
    return ParsedResponse(
        output=plan,
        usage=_usage(),
        model="fake-model",
        request_id=None,
        stop_reason="end_turn",
    )


def _parsed_review(
    verdict: Literal["accept", "revise", "escalate"],
) -> ParsedResponse[Review]:
    return ParsedResponse(
        output=Review(verdict=verdict, feedback="feedback", issues=[]),
        usage=_usage(),
        model="fake-model",
        request_id=None,
        stop_reason="end_turn",
    )


def _engine(
    responses: list[ParsedResponse[Plan] | ParsedResponse[Review] | LLMStop],
) -> LeadEngineer:
    return LeadEngineer(
        FakeLLM([], parsed_responses=responses),
        CommandPolicy.from_default(),
    )


def _ticket() -> TicketInput:
    return TicketInput(id="t1", title="Ship", body="Make the tests pass")


def _repo_context() -> RepoContext:
    return RepoContext(readme=None, tree="src/", files=(), omitted=())


async def test_valid_plan_parses() -> None:
    fake = FakeLLM([], parsed_responses=[_parsed_plan(_plan(test_command=["pytest", "-q"]))])
    engineer = LeadEngineer(fake, CommandPolicy.from_default())

    plan = await engineer.plan_ticket(_ticket(), _repo_context())

    assert plan.steps[0].test_command == ["pytest", "-q"]
    assert len(fake.parse_calls) == 1
    assert fake.parse_calls[0].enable_fallback is True


async def test_invalid_test_command_triggers_exactly_one_retry_then_succeeds() -> None:
    fake = FakeLLM(
        [],
        parsed_responses=[
            _parsed_plan(_plan(test_command=["git", "status"])),
            _parsed_plan(_plan(test_command=["pytest", "-q"])),
        ],
    )
    engineer = LeadEngineer(fake, CommandPolicy.from_default())

    plan = await engineer.plan_ticket(_ticket(), _repo_context())

    assert plan.steps[0].test_command == ["pytest", "-q"]
    assert len(fake.parse_calls) == 2
    assert fake.parse_calls[0].enable_fallback is True
    assert fake.parse_calls[1].enable_fallback is True
    # The retry message is a new user turn and wraps the policy reasons.
    assert fake.parse_calls[1].messages[-1].role == "user"
    assert 'source="gaia:command-policy"' in fake.parse_calls[1].messages[-1].content
    # Lead review 11 (L2): the rejected plan is replayed so the model can revise it.
    assert [m.role for m in fake.parse_calls[1].messages] == ["user", "assistant", "user"]
    assert '"git"' in fake.parse_calls[1].messages[1].content
    assert len(fake.parse_calls[0].messages) == 1


async def test_invalid_test_command_twice_raises_plan_rejected() -> None:
    fake = FakeLLM(
        [],
        parsed_responses=[
            _parsed_plan(_plan(test_command=["git", "status"])),
            _parsed_plan(_plan(test_command=["git", "status"])),
        ],
    )
    engineer = LeadEngineer(fake, CommandPolicy.from_default())

    with pytest.raises(PlanRejected) as caught:
        await engineer.plan_ticket(_ticket(), _repo_context())

    assert len(fake.parse_calls) == 2
    assert "git" in caught.value.reasons[0]


async def test_revise_verdict_round_trips() -> None:
    fake = FakeLLM([], parsed_responses=[_parsed_review("revise")])
    engineer = LeadEngineer(fake, CommandPolicy.from_default())
    plan = _plan(test_command=None)

    review = await engineer.review_step(plan, 0, "diff --git a/a.py b/a.py\n+x", "passed")

    assert review.verdict == "revise"
    assert review.feedback == "feedback"
    assert fake.parse_calls[0].enable_fallback is True


async def test_final_review_round_trips() -> None:
    fake = FakeLLM([], parsed_responses=[_parsed_review("accept")])
    engineer = LeadEngineer(fake, CommandPolicy.from_default())

    review = await engineer.final_review(_plan(test_command=None), "diff --git a/a.py b/a.py\n+x")

    assert review.verdict == "accept"


async def test_final_review_includes_each_steps_checks() -> None:
    fake = FakeLLM([], parsed_responses=[_parsed_review("accept")])
    engineer = LeadEngineer(fake, CommandPolicy.from_default())
    long_output = "x" * (EARLIER_CHECK_OUTPUT_CAP + 10)

    await engineer.final_review(
        _plan(test_command=None),
        "diff --git a/a.py b/a.py\n+x",
        {
            1: CheckOutcome(passed=False, output="1 failed"),
            0: CheckOutcome(passed=True, output=long_output),
        },
    )

    content = fake.parse_calls[0].messages[0].content
    assert "Step 0 checks passed, run when the step was accepted:" in content
    assert "Step 1 checks failed, run on the final code:" in content
    assert content.index("Step 0 checks") < content.index("Step 1 checks")
    assert '<untrusted_content source="test-output:step:1">' in content
    assert "[test output truncated: 10 characters omitted]" in content


async def test_final_review_says_when_no_checks_were_recorded() -> None:
    fake = FakeLLM([], parsed_responses=[_parsed_review("accept")])
    engineer = LeadEngineer(fake, CommandPolicy.from_default())

    await engineer.final_review(_plan(test_command=None), "diff --git a/a.py b/a.py\n+x")

    content = fake.parse_calls[0].messages[0].content
    assert "Step checks: none were recorded for this run." in content


async def test_refusal_escalates_without_retrying() -> None:
    fake = FakeLLM(
        [],
        parsed_responses=[
            LLMStop("refusal", refusal_category="cyber", request_id="req_refused"),
            _parsed_plan(_plan(test_command=None)),
        ],
    )
    engineer = LeadEngineer(fake, CommandPolicy.from_default())

    with pytest.raises(LeadEscalation) as caught:
        await engineer.plan_ticket(_ticket(), _repo_context())

    assert caught.value.category == "cyber"
    assert caught.value.request_id == "req_refused"
    assert len(fake.parse_calls) == 1


async def test_review_refusal_escalates() -> None:
    fake = FakeLLM(
        [],
        parsed_responses=[LLMStop("refusal", refusal_category="policy", request_id="req_r")],
    )
    engineer = LeadEngineer(fake, CommandPolicy.from_default())

    with pytest.raises(LeadEscalation):
        await engineer.review_step(_plan(test_command=None), 0, "diff", None)


async def test_non_refusal_llm_stop_propagates() -> None:
    fake = FakeLLM([], parsed_responses=[LLMStop("max_tokens", request_id="req_max")])
    engineer = LeadEngineer(fake, CommandPolicy.from_default())

    with pytest.raises(LLMStop) as caught:
        await engineer.plan_ticket(_ticket(), _repo_context())

    assert caught.value.stop_reason == "max_tokens"


async def test_review_step_diff_truncation_appends_note() -> None:
    fake = FakeLLM([], parsed_responses=[_parsed_review("accept")])
    engineer = LeadEngineer(fake, CommandPolicy.from_default())
    diff = "diff --git a/a.py b/a.py\n" + ("x" * 25_000)

    await engineer.review_step(_plan(test_command=None), 0, diff, None)

    content = fake.parse_calls[0].messages[0].content
    assert "[truncated:" in content


async def test_review_step_test_output_truncation_keeps_tail() -> None:
    fake = FakeLLM([], parsed_responses=[_parsed_review("accept")])
    engineer = LeadEngineer(fake, CommandPolicy.from_default())
    test_output = "y" * 20_001

    await engineer.review_step(_plan(test_command=None), 0, "diff", test_output)

    content = fake.parse_calls[0].messages[0].content
    assert "[test output truncated: 1 characters omitted]" in content
    assert "y" * 20_000 in content


def test_system_prompts_are_byte_stable() -> None:
    prompts_dir = Path(__file__).parents[1] / "gaia" / "orchestrator" / "prompts"
    plan_prompt = (prompts_dir / "lead_plan.md").read_text(encoding="utf-8")
    review_prompt = (prompts_dir / "lead_review.md").read_text(encoding="utf-8")

    assert lead.PLAN_SYSTEM_PROMPT == plan_prompt
    assert lead.REVIEW_SYSTEM_PROMPT == review_prompt
    # No volatile values are allowed in the cached system prompt.
    for prompt in (lead.PLAN_SYSTEM_PROMPT, lead.REVIEW_SYSTEM_PROMPT):
        assert "{ticket" not in prompt
        assert "REQ_" not in prompt
        assert "2026" not in prompt


async def test_every_untrusted_part_is_wrapped() -> None:
    fake = FakeLLM([], parsed_responses=[_parsed_plan(_plan(test_command=None))])
    engineer = LeadEngineer(fake, CommandPolicy.from_default())

    await engineer.plan_ticket(_ticket(), _repo_context())

    content = fake.parse_calls[0].messages[0].content
    assert '<untrusted_content source="ticket:t1">' in content
    assert '<untrusted_content source="repo-context">' in content

    fake = FakeLLM([], parsed_responses=[_parsed_review("revise")])
    engineer = LeadEngineer(fake, CommandPolicy.from_default())
    await engineer.review_step(_plan(test_command=None), 0, "diff --git a/a.py b/a.py\n+x", "ok")
    content = fake.parse_calls[0].messages[0].content
    assert '<untrusted_content source="diff:step:0">' in content
    assert '<untrusted_content source="test-output:step:0">' in content

    fake = FakeLLM([], parsed_responses=[_parsed_review("accept")])
    engineer = LeadEngineer(fake, CommandPolicy.from_default())
    await engineer.final_review(_plan(test_command=None), "diff --git a/a.py b/a.py\n+x")
    content = fake.parse_calls[0].messages[0].content
    assert '<untrusted_content source="diff:final">' in content


def test_truncate_diff_per_file_and_total() -> None:
    diff = (
        "diff --git a/a.py b/a.py\n"
        + ("x" * 30_000)
        + "\n"
        + "diff --git a/b.py b/b.py\n"
        + "y\n"
        + "diff --git a/c.py b/c.py\n"
        + "z\n"
    )

    text, notes = truncate_diff(diff, per_file=20_000, total=20_010)

    assert len(text) <= 25_000
    assert "a.py (30.026 KB → 20 KB)" in notes
    assert any("files omitted" in note for note in notes)
    assert "b.py" not in text


def test_truncate_diff_no_truncation_needed() -> None:
    diff = "diff --git a/a.py b/a.py\nx\n"

    text, notes = truncate_diff(diff, per_file=100, total=100)

    assert text == diff
    assert notes == []


def test_truncate_diff_formats_small_counts_in_bytes() -> None:
    diff = "diff --git a/a.py b/a.py\n" + ("x" * 800)

    text, notes = truncate_diff(diff, per_file=500, total=1000)

    assert len(text) <= 1000
    assert "a.py (825 B → 500 B)" in notes


def test_truncate_diff_empty() -> None:
    text, notes = truncate_diff("", per_file=10, total=10)

    assert text == ""
    assert notes == []


async def test_plan_retry_message_uses_trusted_wrapper_for_policy_reasons() -> None:
    fake = FakeLLM(
        [],
        parsed_responses=[
            _parsed_plan(_plan(test_command=["git", "status"])),
            _parsed_plan(_plan(test_command=None)),
        ],
    )
    engineer = LeadEngineer(fake, CommandPolicy.from_default())

    await engineer.plan_ticket(_ticket(), _repo_context())

    retry = fake.parse_calls[1].messages[-1].content
    assert "The plan was rejected by the command policy" in retry
    assert '<untrusted_content source="gaia:command-policy">' in retry
    assert "</untrusted_content>" in retry


def test_a_test_command_that_would_need_approval_is_rejected() -> None:
    """Lead review 12b-1 X1: an ``ask`` test command is sent back for revision, and
    rejected if the lead insists, so a plan cannot smuggle ``bash -c …`` into a run."""
    engineer = LeadEngineer(FakeLLM([]), CommandPolicy.from_default())

    [shell] = engineer._denied_test_commands(_plan(["bash", "-c", "make test"]), ())
    assert "would need human approval" in shell
    [script] = engineer._denied_test_commands(_plan(["python", "script.py"]), ())
    assert "would need human approval" in script
    assert engineer._denied_test_commands(_plan(["pytest", "-q"]), ()) == []
    # The owner's registered command is still allowed through extra_allow.
    assert (
        engineer._denied_test_commands(
            _plan(["python", "-m", "pytest", "-q"]), (("python", "-m", "pytest"),)
        )
        == []
    )


def test_plan_test_commands_must_start_with_the_registered_command() -> None:
    """First live end-to-end run: a plan used bare ``pytest`` while the repo registered
    ``uv run pytest -q``; the check then failed with "command not found"."""
    engineer = LeadEngineer(FakeLLM([]), CommandPolicy.from_default())
    registered = ("uv", "run", "pytest", "-q")

    [reason] = engineer._denied_test_commands(
        _plan(["pytest", "-q", "tests/test_greet.py"]), (registered,), registered
    )
    assert "must start with the repository's registered test command" in reason
    assert (
        engineer._denied_test_commands(
            _plan(["uv", "run", "pytest", "-q", "tests/test_greet.py"]), (registered,), registered
        )
        == []
    )


async def test_plan_ticket_tells_the_lead_the_registered_test_command() -> None:
    fake = FakeLLM([], parsed_responses=[_parsed_plan(_plan(["uv", "run", "pytest", "-q", "t"]))])
    engineer = LeadEngineer(fake, CommandPolicy.from_default())

    await engineer.plan_ticket(
        _ticket(),
        _repo_context(),
        extra_allow=(("uv", "run", "pytest", "-q"),),
        test_command=["uv", "run", "pytest", "-q"],
    )

    [call] = fake.parse_calls
    assert '["uv", "run", "pytest", "-q"]' in call.messages[0].content
