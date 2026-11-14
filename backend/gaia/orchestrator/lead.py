"""Lead planning and review through single structured-output calls (doc 11).

The two system prompts are lead-owned files. They are read once at import time
and never formatted, so the byte stream is stable across calls and Anthropic's
prompt cache can keep hitting it. Everything volatile goes in the user message.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from pathlib import Path
from typing import TypeVar

from pydantic import BaseModel

from gaia.domain.plans import Plan, Review, TicketInput
from gaia.llm.base import LLMMessage, LLMStop, StructuredLLM
from gaia.llm.untrusted import wrap_untrusted
from gaia.orchestrator.repo_context import RepoContext
from gaia.security.commands import CommandPolicy

_PROMPTS_DIR = Path(__file__).with_name("prompts")
PLAN_SYSTEM_PROMPT = (_PROMPTS_DIR / "lead_plan.md").read_text(encoding="utf-8")
REVIEW_SYSTEM_PROMPT = (_PROMPTS_DIR / "lead_review.md").read_text(encoding="utf-8")

#: Diff truncation caps from doc 11 task 2.
DIFF_PER_FILE_CAP = 20_000
DIFF_TOTAL_CAP = 150_000
TEST_OUTPUT_CAP = 20_000

_StructuredT = TypeVar("_StructuredT", bound=BaseModel)


class LeadEscalation(Exception):
    """The lead refused or cannot safely decide; a human must take over."""

    def __init__(self, category: str, request_id: str | None) -> None:
        self.category = category
        self.request_id = request_id
        suffix = f" (request {request_id})" if request_id is not None else ""
        super().__init__(f"lead escalated: {category}{suffix}")


class PlanRejected(Exception):
    """The lead returned a plan whose commands the policy denied twice."""

    def __init__(self, reasons: Sequence[str]) -> None:
        self.reasons = tuple(reasons)
        super().__init__("lead plan rejected: " + "; ".join(self.reasons))


def truncate_diff(diff: str, *, per_file: int, total: int) -> tuple[str, list[str]]:
    """Cap ``diff`` per file and in total, returning the text and truncation notes.

    Files are split on ``diff --git`` headers. A file longer than ``per_file``
    keeps its first ``per_file`` characters. Once ``total`` characters are
    reached, the remaining whole files are omitted. The returned notes are plain
    strings such as ``"a.py (40 KB → 20 KB)"`` or ``"3 files omitted"``.
    """
    chunks = _split_diff(diff)
    if not chunks:
        return "", []

    kept: list[str] = []
    notes: list[str] = []
    used = 0
    for index, chunk in enumerate(chunks):
        name = _diff_file_name(chunk)
        label = name if name is not None else "preamble"
        content = chunk[:per_file]
        if used + len(content) > total:
            omitted = len(chunks) - index
            notes.append("1 file omitted" if omitted == 1 else f"{omitted} files omitted")
            break
        kept.append(content)
        used += len(content)
        if len(chunk) > per_file:
            notes.append(f"{label} ({_format_chars(len(chunk))} → {_format_chars(per_file)})")
    return "".join(kept), notes


def _split_diff(diff: str) -> list[str]:
    """Split ``diff`` into one chunk per ``diff --git`` header."""
    if not diff:
        return []
    matches = list(re.finditer(r"^diff --git ", diff, re.MULTILINE))
    if not matches:
        return [diff]
    chunks: list[str] = []
    start = 0
    for match in matches:
        if match.start() > start:
            chunks.append(diff[start : match.start()])
        start = match.start()
    chunks.append(diff[start:])
    return [chunk for chunk in chunks if chunk]


def _diff_file_name(chunk: str) -> str | None:
    """Return the ``a/`` path from a git diff header, when one exists."""
    match = re.match(r"^diff --git a/(\S+) b/(\S+)", chunk)
    return match.group(1) if match is not None else None


def _format_chars(count: int) -> str:
    """Format a character count the way the truncation note examples do."""
    if count >= 1000:
        return f"{count / 1000:g} KB"
    return f"{count} B"


class LeadEngineer:
    """The lead's two structured-output calls: plan and review."""

    def __init__(
        self,
        llm: StructuredLLM,
        policy: CommandPolicy,
        *,
        repo_context_budget_tokens: int = 100_000,
    ) -> None:
        self._llm = llm
        self._policy = policy
        self._repo_context_budget_tokens = repo_context_budget_tokens

    async def plan_ticket(
        self,
        ticket: TicketInput,
        repo_context: RepoContext,
        *,
        extra_allow: Sequence[Sequence[str]] = (),
    ) -> Plan:
        """Turn one ticket into a :class:`Plan`, retrying denied commands once."""
        user_content = (
            wrap_untrusted(_render_ticket(ticket), f"ticket:{ticket.id}")
            + "\n\n"
            + wrap_untrusted(repo_context.render(), "repo-context")
        )
        messages = [LLMMessage(role="user", content=user_content)]
        plan = await self._parse_structured(PLAN_SYSTEM_PROMPT, messages, Plan)
        denied = self._denied_test_commands(plan, extra_allow)
        if not denied:
            return plan

        # The model must see the plan it is asked to revise: replay it as the
        # assistant turn, then send the policy's objections (lead review 11, L2).
        messages.append(LLMMessage(role="assistant", content=plan.model_dump_json()))
        messages.append(
            LLMMessage(
                role="user",
                content=(
                    "The plan was rejected by the command policy. Revise the "
                    "`test_command` fields and return a valid Plan.\n"
                    + wrap_untrusted("\n".join(denied), "gaia:command-policy")
                ),
            )
        )
        plan = await self._parse_structured(PLAN_SYSTEM_PROMPT, messages, Plan)
        denied = self._denied_test_commands(plan, extra_allow)
        if denied:
            raise PlanRejected(denied)
        return plan

    async def review_step(
        self,
        plan: Plan,
        step_index: int,
        diff: str,
        test_output: str | None,
    ) -> Review:
        """Review one step's diff and test output."""
        step = plan.steps[step_index]
        parts = [
            f"Plan:\n{plan.model_dump_json(indent=2)}",
            (
                f"Step {step_index}\nTitle: {step.title}\n"
                f"Instructions:\n{step.instructions}\n"
                "Acceptance:\n" + "\n".join(f"- {item}" for item in step.acceptance)
            ),
            wrap_untrusted(_truncate_diff(diff), f"diff:step:{step_index}"),
        ]
        if test_output is not None:
            parts.append(
                wrap_untrusted(_truncate_test_output(test_output), f"test-output:step:{step_index}")
            )
        return await self._parse_structured(
            REVIEW_SYSTEM_PROMPT,
            [LLMMessage(role="user", content="\n\n".join(parts))],
            Review,
        )

    async def final_review(self, plan: Plan, full_diff: str) -> Review:
        """Review the whole final diff."""
        parts = [
            f"Plan:\n{plan.model_dump_json(indent=2)}",
            wrap_untrusted(_truncate_diff(full_diff), "diff:final"),
        ]
        return await self._parse_structured(
            REVIEW_SYSTEM_PROMPT,
            [LLMMessage(role="user", content="\n\n".join(parts))],
            Review,
        )

    async def _parse_structured(
        self,
        system: str,
        messages: Sequence[LLMMessage],
        output_model: type[_StructuredT],
    ) -> _StructuredT:
        """Call the LLM with fallback enabled and escalate refusals."""
        try:
            response = await self._llm.parse(
                system=system,
                messages=messages,
                output_model=output_model,
                enable_fallback=True,
            )
        except LLMStop as exc:
            if exc.stop_reason == "refusal":
                raise LeadEscalation(
                    category=exc.refusal_category or "unspecified",
                    request_id=exc.request_id,
                ) from exc
            raise
        return response.output

    def _denied_test_commands(self, plan: Plan, extra_allow: Sequence[Sequence[str]]) -> list[str]:
        """Return one human-readable reason per denied ``test_command``."""
        denied: list[str] = []
        for index, step in enumerate(plan.steps, start=1):
            if step.test_command is None:
                continue
            decision = self._policy.evaluate(step.test_command, extra_allow=extra_allow)
            if decision.action == "deny":
                denied.append(f"step {index} test_command {step.test_command!r}: {decision.reason}")
        return denied


def _render_ticket(ticket: TicketInput) -> str:
    """Render the ticket fields for the untrusted user message."""
    return f"Ticket {ticket.id}\nTitle: {ticket.title}\nBody:\n{ticket.body}"


def _truncate_diff(diff: str) -> str:
    """Truncate a diff and append the plain note the model is told to expect."""
    text, notes = truncate_diff(diff, per_file=DIFF_PER_FILE_CAP, total=DIFF_TOTAL_CAP)
    if notes:
        text += f"\n[truncated: {', '.join(notes)}]"
    return text


def _truncate_test_output(test_output: str) -> str:
    """Keep the last :data:`TEST_OUTPUT_CAP` characters of test output."""
    if len(test_output) <= TEST_OUTPUT_CAP:
        return test_output
    dropped = len(test_output) - TEST_OUTPUT_CAP
    return (
        test_output[-TEST_OUTPUT_CAP:] + f"\n[test output truncated: {dropped} characters omitted]"
    )
