"""Opt-in live test for the Claude Agent SDK runtime (doc 10c).

The lead runs this against a real gateway and a real ``srt`` binary:

    GAIA_LIVE_CLAUDE_SDK=1 GAIA_SRT_PATH=/path/to/srt \\
    GAIA_LIVE_DATA_DIR=/tmp/gaia-live-data uv run pytest tests/test_claude_sdk_live.py
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from gaia.config.settings import Settings
from tests.runtime_contract import base_sha, make_worktree_location


@pytest.mark.live
async def test_real_claude_sdk_run_writes_file_through_gaia_tool(
    git_repo: Path,
    tmp_path: Path,
) -> None:
    """Run one real SDK task and assert ``mcp__gaia__write_file`` wrote a file."""
    if os.environ.get("GAIA_LIVE_CLAUDE_SDK") != "1" or not os.environ.get("GAIA_SRT_PATH"):
        pytest.skip("set GAIA_LIVE_CLAUDE_SDK=1 and GAIA_SRT_PATH")

    from gaia.api.app import create_app
    from gaia.db.engine import session_scope
    from gaia.db.models import Repository, Run, Ticket
    from gaia.db.repo import ScopedRepo
    from gaia.domain.enums import AgentRole
    from gaia.runtimes.approvals import DbApprovalBroker
    from gaia.runtimes.base import AgentTask
    from gaia.runtimes.claude_agent_sdk import ClaudeAgentSdkRuntime
    from gaia.runtimes.support import run_with_limits
    from gaia.security.audit import DbAuditSink
    from gaia.security.commands import CommandPolicy
    from gaia.security.sandbox import find_srt
    from gaia.services.events import BusEventSink, EventSink
    from gaia.tools.fs import find_rg
    from gaia.tools.registry import ALL_TOOLS, ToolContext, build_default_registry
    from gaia.tools.shell import SrtCommandRunner

    data_dir = Path(os.environ.get("GAIA_LIVE_DATA_DIR", tmp_path / "gaia-live-data"))
    settings = Settings(
        data_dir=data_dir,
        srt_path=Path(os.environ["GAIA_SRT_PATH"]),
    )
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        services = app.state.services
        worktree = make_worktree_location(git_repo, tmp_path)
        current_sha = await services.git.current_sha(worktree)
        rg_path = find_rg(settings)
        registry = build_default_registry(search_supports_regex=rg_path is not None).subset(
            ALL_TOOLS
        )

        async with session_scope(services.sessions) as session:
            repos = ScopedRepo(session, Repository, services.principal.workspace_id)
            repository = await repos.create(
                name=f"live-claude-sdk-{current_sha[:8]}",
                local_path=str(worktree.work_tree),
                created_by=services.principal.user_id,
            )
            tickets = ScopedRepo(session, Ticket, services.principal.workspace_id)
            ticket = await tickets.create(
                repo_id=repository.id,
                title="Live Claude Agent SDK run",
                created_by=services.principal.user_id,
            )
            runs = ScopedRepo(session, Run, services.principal.workspace_id)
            run = await runs.create(
                ticket_id=ticket.id,
                repo_id=repository.id,
                created_by=services.principal.user_id,
            )

        async def context_factory(
            task: AgentTask, events: EventSink, task_dir: Path
        ) -> ToolContext:
            return ToolContext(
                task=task,
                git=services.git,
                policy=CommandPolicy.from_default(),
                approvals=DbApprovalBroker(services.sessions, services.events),
                audit=DbAuditSink(services.sessions),
                events=events,
                commands=SrtCommandRunner(settings, srt_path=find_srt(settings)),
                task_dir=task_dir,
                actor=f"live:{task.task_id}",
                approval_timeout_s=600,
                rg_path=rg_path,
            )

        task = AgentTask(
            task_id=run.id,
            run_id=run.id,
            run_step_id=None,
            workspace_id=services.principal.workspace_id,
            agent_id=None,
            role=AgentRole.LEAD,
            instructions=(
                "Write a file named live-output.txt containing exactly 'live ok\\n'. "
                "Then call the finish tool."
            ),
            worktree=worktree,
            base_sha=base_sha(git_repo),
        )
        runtime = ClaudeAgentSdkRuntime(
            registry=registry,
            settings=settings,
            sessions=services.sessions,
            context_factory=context_factory,
            model=settings.lead_model,
            price_table=services.prices,
        )
        sink = BusEventSink(
            services.events,
            workspace_id=services.principal.workspace_id,
            run_id=run.id,
        )

        result = await run_with_limits(runtime, task, sink, git=services.git)

    assert result.status == "completed", result
    written = worktree.work_tree / "live-output.txt"
    assert written.read_text(encoding="utf-8") == "live ok\n"
    assert "live-output.txt" in result.files_changed
