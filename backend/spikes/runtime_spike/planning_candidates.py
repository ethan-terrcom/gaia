"""Doc 08 spike: static-context planning (one structured call) vs agentic planning (Agent SDK, read-only).

usage: planning_candidates.py static|agentic <repo> <ticket.md> <lead_plan.md>
Env: ANTHROPIC_BASE_URL (gateway) and ANTHROPIC_API_KEY (a gaia_rt_ run token) only.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from pathlib import Path

from pydantic import BaseModel


class PlanStep(BaseModel):
    title: str
    instructions: str
    files_likely_touched: list[str]
    acceptance: list[str]
    test_command: list[str] | None


class Plan(BaseModel):
    summary: str
    risks: list[str]
    steps: list[PlanStep]


def static_context(repo: Path, ticket: str) -> str:
    tree = sorted(
        str(p.relative_to(repo))
        for p in repo.rglob("*")
        if ".git" not in p.parts and ".venv" not in p.parts and "__pycache__" not in p.parts
    )
    named = [p for p in tree if p in ticket]
    files = "\n".join(f"--- {p}\n{(repo / p).read_text()}" for p in named if (repo / p).is_file())
    readme = (repo / "README.md").read_text()
    return (
        f'<untrusted_content source="ticket">\n{ticket}\n</untrusted_content>\n'
        f'<untrusted_content source="repo">\nREADME:\n{readme}\nTREE:\n'
        + "\n".join(tree)
        + f"\nFILES:\n{files}\n</untrusted_content>"
    )


async def static(repo: Path, ticket: str, system: str) -> dict[str, object]:
    import anthropic

    client = anthropic.AsyncAnthropic()  # base URL and token from env
    start = time.time()
    # Streamed: a long non-streamed call idles on the wire while the model thinks, and the
    # first attempt through the gateway hung for the SDK's full 10-minute timeout.
    async with client.messages.stream(
        model="claude-opus-5-5",
        max_tokens=8000,
        system=system,
        messages=[{"role": "user", "content": static_context(repo, ticket)}],
        output_format=Plan,
        output_config={"effort": "high"},
    ) as stream:
        msg = await stream.get_final_message()
    return {
        "wall_s": round(time.time() - start, 1),
        "stop_reason": msg.stop_reason,
        "usage": msg.usage.model_dump(),
        "plan": msg.parsed_output.model_dump() if msg.parsed_output else None,
    }


async def agentic(repo: Path, ticket: str, system: str) -> dict[str, object]:
    from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient, ResultMessage

    options = ClaudeAgentOptions(
        cwd=str(repo),
        model="claude-opus-5-5",
        setting_sources=[],
        tools=["Read", "Glob", "Grep"],
        allowed_tools=["Read", "Glob", "Grep"],
        disallowed_tools=["Bash", "Write", "Edit", "WebFetch", "WebSearch", "Task", "NotebookEdit"],
        permission_mode="dontAsk",
        max_turns=20,
        max_budget_usd=1.0,
        system_prompt=system,
        output_format={"type": "json_schema", "schema": Plan.model_json_schema()},
    )
    start = time.time()
    result = None
    async with ClaudeSDKClient(options=options) as client:
        await client.query(
            "Explore the repository read-only as needed, then return the plan for this ticket.\n"
            f'<untrusted_content source="ticket">\n{ticket}\n</untrusted_content>'
        )
        async for message in client.receive_response():
            if isinstance(message, ResultMessage):
                result = message
    structured = getattr(result, "structured_output", None) if result else None
    return {
        "wall_s": round(time.time() - start, 1),
        "num_turns": result.num_turns if result else None,
        "sdk_cost_usd": result.total_cost_usd if result else None,
        "is_error": result.is_error if result else None,
        "plan": structured,
    }


async def main(ticket: str, system: str) -> None:
    mode, repo = sys.argv[1], Path(sys.argv[2])
    out = await (static if mode == "static" else agentic)(repo, ticket, system)
    print(
        json.dumps(
            {"mode": mode, "env_key_like": [k for k in os.environ if "KEY" in k], **out},
            default=str,
        )
    )


if __name__ == "__main__":
    asyncio.run(main(Path(sys.argv[3]).read_text(), Path(sys.argv[4]).read_text()))
