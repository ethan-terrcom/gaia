"""GatewayPlanner: loads a run and plans through the lead LLM (12b-2)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from gaia.config.settings import Settings
from gaia.db.engine import session_scope
from gaia.db.models import Run
from gaia.db.repo import ScopedRepo
from gaia.domain.plans import Plan, PlanStep, TicketInput
from gaia.llm.base import LLMUsage, ParsedResponse
from gaia.llm.fake import FakeLLM
from gaia.orchestrator.impl import planning
from gaia.orchestrator.impl.planning import GatewayPlanner
from gaia.orchestrator.repo_context import RepoContext
from gaia.services.costs import PriceTable
from tests.db_builders import make_chain

PLAN = Plan(
    summary="Add a test",
    risks=[],
    steps=[
        PlanStep(
            title="Write a test",
            instructions="Add a failing test and make it pass.",
            files_likely_touched=["tests/test_x.py"],
            acceptance=["pytest passes"],
            test_command=["uv", "run", "pytest", "-q"],
        )
    ],
)


def _assigned(plan: Plan, worker: str = "deepseek-v4-pro") -> Plan:
    """``plan`` with every step pinned to ``worker``, as the planner returns it (19b)."""
    return plan.model_copy(
        update={"steps": [step.model_copy(update={"worker": worker}) for step in plan.steps]}
    )


def _parsed_plan() -> ParsedResponse[Plan]:
    return ParsedResponse(
        output=PLAN,
        usage=LLMUsage(provider="fake", model="fake-model", input_tokens=10, output_tokens=5),
        model="fake-model",
        request_id=None,
        stop_reason="end_turn",
    )


@asynccontextmanager
async def _fake_lead_llm(
    calls: list[dict[str, Any]],
    **kwargs: Any,
) -> AsyncIterator[FakeLLM]:
    calls.append(kwargs)
    yield FakeLLM([], parsed_responses=[_parsed_plan()])


async def _fake_build_repo_context(
    root: Path,
    ticket: TicketInput,
    **kwargs: Any,
) -> RepoContext:
    del kwargs
    return RepoContext(readme=None, tree=f"tree for {root}", files=(), omitted=())


async def _make_planner(
    database: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    settings: Settings,
) -> tuple[GatewayPlanner, list[dict[str, Any]]]:
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(planning, "lead_llm", lambda **kwargs: _fake_lead_llm(calls, **kwargs))
    monkeypatch.setattr(planning, "build_repo_context", _fake_build_repo_context)
    return GatewayPlanner(
        settings=settings,
        sessions=database.sessions,
        price_table=PriceTable({}),
    ), calls


async def test_plan_loads_run_and_returns_a_plan(
    database: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with session_scope(database.sessions) as session:
        chain = await make_chain(session, slug="planning")
        await ScopedRepo(session, Run, chain.workspace.id).update(
            chain.run.id, worktree_id=chain.worktree.id
        )
    settings = Settings(
        planner_provider="deepseek",
        planner_model="deepseek-v4-pro",
        data_dir=tmp_path / "gaia-data",
    )
    planner, calls = await _make_planner(database, tmp_path, monkeypatch, settings=settings)

    outcome = await planner.plan(chain.run.id)

    assert outcome.plan == _assigned(PLAN)
    assert outcome.skip_gate is False
    assert len(calls) == 1
    assert calls[0]["run_id"] == chain.run.id
    assert calls[0]["workspace_id"] == chain.workspace.id
    assert calls[0]["settings"] is settings


async def test_plan_has_no_db_session_open_during_the_llm_call(
    database: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The gateway/lead call happens after the loading session has closed."""
    async with session_scope(database.sessions) as session:
        chain = await make_chain(session, slug="planning-session")
    settings = Settings(
        planner_provider="anthropic",
        planner_model="claude-opus-5-5",
        data_dir=tmp_path / "gaia-data",
    )
    active = 0
    real_session_scope = session_scope

    @asynccontextmanager
    async def tracking_session_scope(factory: Any) -> AsyncIterator[AsyncSession]:
        nonlocal active
        active += 1
        try:
            async with real_session_scope(factory) as session:
                yield session
        finally:
            active -= 1

    observed: dict[str, int] = {}

    @asynccontextmanager
    async def fake_lead_llm(**kwargs: Any) -> AsyncIterator[FakeLLM]:
        observed["active_sessions"] = active
        yield FakeLLM([], parsed_responses=[_parsed_plan()])

    monkeypatch.setattr(planning, "session_scope", tracking_session_scope)
    monkeypatch.setattr(planning, "lead_llm", fake_lead_llm)
    monkeypatch.setattr(planning, "build_repo_context", _fake_build_repo_context)
    planner = GatewayPlanner(
        settings=settings,
        sessions=database.sessions,
        price_table=PriceTable({}),
    )

    await planner.plan(chain.run.id)

    assert observed["active_sessions"] == 0


async def test_plan_missing_run_raises(
    database: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings(data_dir=tmp_path / "gaia-data")
    planner, _ = await _make_planner(database, tmp_path, monkeypatch, settings=settings)

    with pytest.raises(ValueError, match="no run"):
        await planner.plan("missing")


async def test_plan_missing_ticket_raises(
    database: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with session_scope(database.sessions) as session:
        chain = await make_chain(session, slug="planning-ticket")
        other_chain = await make_chain(session, slug="other")
        # A cross-workspace ticket id is a valid FK but invisible to the scoped
        # repo, so the planner must report it exactly like a missing ticket.
        await ScopedRepo(session, Run, chain.workspace.id).update(
            chain.run.id, ticket_id=other_chain.ticket.id
        )
    settings = Settings(data_dir=tmp_path / "gaia-data")
    planner, _ = await _make_planner(database, tmp_path, monkeypatch, settings=settings)

    with pytest.raises(ValueError, match="has no ticket"):
        await planner.plan(chain.run.id)


async def test_plan_missing_repository_raises(
    database: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with session_scope(database.sessions) as session:
        chain = await make_chain(session, slug="planning-repo")
        other_chain = await make_chain(session, slug="other-repo")
        await ScopedRepo(session, Run, chain.workspace.id).update(
            chain.run.id, repo_id=other_chain.repository.id
        )
    settings = Settings(data_dir=tmp_path / "gaia-data")
    planner, _ = await _make_planner(database, tmp_path, monkeypatch, settings=settings)

    with pytest.raises(ValueError, match="has no repository"):
        await planner.plan(chain.run.id)


async def test_plan_missing_worktree_raises(
    database: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with session_scope(database.sessions) as session:
        chain = await make_chain(session, slug="planning-worktree")
        await ScopedRepo(session, Run, chain.workspace.id).update(chain.run.id, worktree_id=None)
    settings = Settings(data_dir=tmp_path / "gaia-data")
    planner, _ = await _make_planner(database, tmp_path, monkeypatch, settings=settings)

    with pytest.raises(ValueError, match="has no worktree"):
        await planner.plan(chain.run.id)


async def test_plan_worktree_row_from_another_workspace_raises(
    database: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with session_scope(database.sessions) as session:
        chain = await make_chain(session, slug="planning-worktree-row")
        other_chain = await make_chain(session, slug="other-worktree-row")
        await ScopedRepo(session, Run, chain.workspace.id).update(
            chain.run.id, worktree_id=other_chain.worktree.id
        )
    settings = Settings(data_dir=tmp_path / "gaia-data")
    planner, _ = await _make_planner(database, tmp_path, monkeypatch, settings=settings)

    with pytest.raises(ValueError, match="has no worktree"):
        await planner.plan(chain.run.id)


async def test_lead_llm_revokes_even_when_client_has_no_aclose(
    database: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from gaia.db.models import RunToken
    from gaia.orchestrator.impl import lead_llm as lead_llm_module

    async with session_scope(database.sessions) as session:
        chain = await make_chain(session, slug="lead-llm-no-aclose")

    class NoCloseClient:
        async def parse(self, **kwargs: Any) -> None:
            del kwargs

    monkeypatch.setattr(lead_llm_module, "build_structured_llm", lambda **kwargs: NoCloseClient())

    async with lead_llm_module.lead_llm(
        settings=Settings(planner_provider="anthropic", planner_model="claude-opus-5-5"),
        sessions=database.sessions,
        price_table=PriceTable({}),
        run_id=chain.run.id,
        workspace_id=chain.workspace.id,
    ):
        pass

    async with session_scope(database.sessions) as session:
        tokens = await ScopedRepo(session, RunToken, chain.workspace.id).list()
    assert len(tokens) == 1
    assert tokens[0].revoked_at is not None
