"""Agent modes (doc 19b): config, default store, run start, planner, reviewer, routing."""

from __future__ import annotations

import asyncio
import os
import stat
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from typing import Any, cast

import pytest
from pydantic import ValidationError

from gaia.config.secrets import InMemorySecretStore, SecretName
from gaia.config.settings import Settings
from gaia.db.engine import session_scope
from gaia.db.models import AuditLog, Run, RunStep
from gaia.db.repo import AuditLogRepo, ScopedRepo
from gaia.domain.agent_modes import (
    AgentMode,
    AgentModeConfig,
    config_from_json,
    default_agent_config,
)
from gaia.domain.enums import RunStatus, RuntimeKind
from gaia.domain.plans import CrowdPlan, Plan, PlanStep, TicketInput
from gaia.llm.base import LLMBudgetExceeded, LLMUsage, ParsedResponse
from gaia.llm.fake import FakeLLM
from gaia.orchestrator.agent_mode_store import AGENT_MODE_FILE, AgentModeStore
from gaia.orchestrator.deps import PlanCandidate, PlanOutcome
from gaia.orchestrator.impl import planning
from gaia.orchestrator.impl import review as review_module
from gaia.orchestrator.impl.planning import (
    GatewayPlanner,
    assign_workers,
    straight_shot_plan,
    workers_note,
)
from gaia.orchestrator.impl.review import STRAIGHT_SHOT_FINAL_REVIEW, GatewayReviewer
from gaia.orchestrator.lead import SYNTHESIZE_SYSTEM_PROMPT, LeadEngineer
from gaia.orchestrator.nodes.approve_plan import approve_plan
from gaia.orchestrator.nodes.lead_plan import lead_plan
from gaia.orchestrator.repo_context import RepoContext
from gaia.security.commands import CommandPolicy
from gaia.services.agent_modes import (
    UnknownModel,
    catalog_problems,
    legacy_agent_config,
    missing_provider_keys,
    provider_of_model,
    run_agent_config,
    runtime_for_model,
)
from gaia.services.costs import ModelPrice, PriceRates, PriceTable, load_price_table
from gaia.services.git import GitService
from tests.conftest import Api, Database
from tests.db_builders import make_chain
from tests.orchestrator_fakes import FakePlanner, make_plan
from tests.test_api_runs import _manager, _seed_ticket_worktree

pytestmark = pytest.mark.anyio

PRICES = load_price_table()

TIERED = AgentModeConfig(
    mode=AgentMode.TIERED,
    lead_model="claude-opus-5-5",
    worker_models=["deepseek-v4-pro", "gpt-6-luna"],
)
CROWD = AgentModeConfig(
    mode=AgentMode.CROWD,
    lead_model="claude-opus-5-5",
    worker_models=["deepseek-v4-pro"],
    panel_models=["gpt-6.2-sol", "grok-4.7", "deepseek-v4-pro"],
)
SINGLE = AgentModeConfig(mode=AgentMode.SINGLE, lead_model="gpt-6.2-sol")


def _step(title: str = "Step", worker: str | None = None) -> PlanStep:
    return PlanStep(
        title=title,
        instructions="Do it.",
        files_likely_touched=["a.py"],
        acceptance=["it works"],
        worker=worker,
    )


def _plan(*steps: PlanStep, summary: str = "A plan") -> Plan:
    return Plan(summary=summary, risks=[], steps=list(steps) or [_step()])


def _parsed(output: Any) -> ParsedResponse[Any]:
    return ParsedResponse(
        output=output,
        usage=LLMUsage(provider="fake", model="fake", input_tokens=1, output_tokens=1),
        model="fake",
        request_id=None,
        stop_reason="end_turn",
    )


# --- the config ------------------------------------------------------------


def test_every_mode_validates_its_roles() -> None:
    assert SINGLE.models() == ("gpt-6.2-sol",)
    assert CROWD.models() == ("claude-opus-5-5", "deepseek-v4-pro", "gpt-6.2-sol", "grok-4.7")


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        ({"mode": "single", "lead_model": "m", "worker_models": ["w"]}, "one model"),
        ({"mode": "tiered", "lead_model": "m"}, "1 to 4 worker"),
        (
            {"mode": "tiered", "lead_model": "m", "worker_models": ["a", "b", "c", "d", "e"]},
            "1 to 4 worker",
        ),
        (
            {"mode": "tiered", "lead_model": "m", "worker_models": ["w"], "panel_models": ["p"]},
            "no panel",
        ),
        (
            {"mode": "crowd", "lead_model": "m", "worker_models": ["w"], "panel_models": ["p"]},
            "2 to 5 panel",
        ),
        ({"mode": "tiered", "lead_model": "m", "worker_models": ["w", "w"]}, "only once"),
        ({"mode": "tiered", "lead_model": " m", "worker_models": ["w"]}, "invalid model id"),
        ({"mode": "tiered", "lead_model": "m", "worker_models": [""]}, "invalid model id"),
        ({"mode": "tiered", "lead_model": "m", "worker_models": ["w"], "extra": 1}, "extra"),
    ],
)
def test_bad_configs_are_refused(raw: dict[str, Any], message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        AgentModeConfig.model_validate(raw)


def test_worker_for_falls_back_to_the_first_worker() -> None:
    assert TIERED.worker_for("gpt-6-luna") == "gpt-6-luna"
    assert TIERED.worker_for("made-up") == "deepseek-v4-pro"
    assert TIERED.worker_for(None) == "deepseek-v4-pro"
    assert SINGLE.worker_for("anything") == "gpt-6.2-sol"


def test_config_from_json_and_the_default() -> None:
    assert config_from_json(None) is None
    assert config_from_json(TIERED.model_dump()) == TIERED
    assert default_agent_config(lead_model="a", junior_model="b").worker_models == ["b"]


# --- catalog, keys and routing ---------------------------------------------


def test_catalog_problems_name_unknown_models() -> None:
    config = AgentModeConfig(mode=AgentMode.SINGLE, lead_model="not-a-model")

    assert catalog_problems(TIERED, PRICES) == []
    assert catalog_problems(config, PRICES) == ["model 'not-a-model' is not in the model catalog"]
    with pytest.raises(UnknownModel, match="not-a-model"):
        provider_of_model("not-a-model", PRICES)


def test_missing_provider_keys_lists_unset_entries_in_provider_order() -> None:
    store = InMemorySecretStore()
    store.set(SecretName.ANTHROPIC_API_KEY, "sk-ant-test-123")

    assert missing_provider_keys(CROWD, PRICES, store) == [
        SecretName.DEEPSEEK_API_KEY,
        SecretName.OPENAI_API_KEY,
        SecretName.XAI_API_KEY,
    ]
    assert missing_provider_keys(CROWD, PRICES, store, fake_llm=True) == []


def test_legacy_config_reproduces_the_pre_19b_roles(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path, planner_model="deepseek-v4-pro")

    native = legacy_agent_config(settings, None)
    claude = legacy_agent_config(settings, RuntimeKind.CLAUDE_AGENT_SDK.value)

    assert native == AgentModeConfig(
        mode=AgentMode.TIERED, lead_model="deepseek-v4-pro", worker_models=["deepseek-v4-pro"]
    )
    assert claude.worker_models == [settings.lead_model]
    assert run_agent_config(SINGLE.model_dump(), settings=settings, junior_runtime=None) == SINGLE
    assert run_agent_config(None, settings=settings, junior_runtime=None) == native


def test_runtime_for_model_routes_by_provider(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path)
    demo = Settings(data_dir=tmp_path, fake_llm=True)

    assert runtime_for_model("claude-sonnet-5-5", PRICES, settings) == (
        RuntimeKind.CLAUDE_AGENT_SDK,
        "claude-sonnet-5-5",
    )
    assert runtime_for_model("grok-4.7", PRICES, settings) == (
        RuntimeKind.NATIVE_DEEPSEEK,
        "grok-4.7",
    )
    assert runtime_for_model("claude-opus-5-5", PRICES, demo) == (
        RuntimeKind.NATIVE_DEEPSEEK,
        demo.junior_model,
    )


# --- the default store -----------------------------------------------------


def test_store_round_trips_owner_only(tmp_path: Path) -> None:
    store = AgentModeStore(tmp_path / "data" / AGENT_MODE_FILE)

    assert store.read() is None
    store.write(CROWD)

    assert store.read() == CROWD
    mode = stat.S_IMODE(os.stat(tmp_path / "data" / AGENT_MODE_FILE).st_mode)
    assert mode == 0o600


def test_store_ignores_a_bad_or_unreadable_file(tmp_path: Path) -> None:
    bad = tmp_path / AGENT_MODE_FILE
    bad.write_text('{"mode": "turbo"}', encoding="utf-8")
    unreadable = tmp_path / "dir-not-file"
    unreadable.mkdir()

    assert AgentModeStore(bad).read() is None
    assert AgentModeStore(unreadable).read() is None


def test_store_write_cleans_up_after_a_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*args: Any, **kwargs: Any) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", boom)

    with pytest.raises(OSError, match="disk full"):
        AgentModeStore(tmp_path / AGENT_MODE_FILE).write(TIERED)
    assert list(tmp_path.iterdir()) == []


# --- admin routes ----------------------------------------------------------


@pytest.fixture
def modes_api(api: Api) -> Api:
    store = AgentModeStore(api.settings.data_dir / AGENT_MODE_FILE)
    api.app.state.services = replace(api.app.state.services, agent_modes=store)
    return api


async def test_get_agent_mode_falls_back_to_settings(modes_api: Api) -> None:
    response = await modes_api.client.get("/api/v1/admin/agent-mode", headers=modes_api.auth)

    assert response.status_code == 200
    assert response.json() == {
        "config": {
            "mode": "tiered",
            "lead_model": "claude-opus-5-5",
            "worker_models": ["deepseek-v4-pro"],
            "panel_models": [],
        },
        "saved": False,
    }


async def test_put_agent_mode_saves_the_default_and_audits(
    modes_api: Api, database: Database
) -> None:
    first = await modes_api.client.put(
        "/api/v1/admin/agent-mode", json=CROWD.model_dump(mode="json"), headers=modes_api.auth
    )
    second = await modes_api.client.put(
        "/api/v1/admin/agent-mode", json=SINGLE.model_dump(mode="json"), headers=modes_api.auth
    )
    read = await modes_api.client.get("/api/v1/admin/agent-mode", headers=modes_api.auth)

    assert first.status_code == 200
    assert second.json()["saved"] is True
    assert read.json()["config"]["mode"] == "single"
    workspace_id: str = modes_api.app.state.services.principal.workspace_id
    async with database.sessions() as session:
        rows = await AuditLogRepo(session, AuditLog, workspace_id).list(action="admin.agent_mode")
    assert [row.details_json["before"] for row in rows] == [None, CROWD.model_dump(mode="json")]


async def test_put_agent_mode_refuses_unknown_models(modes_api: Api) -> None:
    response = await modes_api.client.put(
        "/api/v1/admin/agent-mode",
        json={"mode": "single", "lead_model": "gpt-99"},
        headers=modes_api.auth,
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "unknown_model"


async def test_agent_mode_routes_are_503_without_the_orchestrator(api: Api) -> None:
    get = await api.client.get("/api/v1/admin/agent-mode", headers=api.auth)
    put = await api.client.put(
        "/api/v1/admin/agent-mode", json=SINGLE.model_dump(mode="json"), headers=api.auth
    )

    assert (get.status_code, put.status_code) == (503, 503)


# --- starting a run --------------------------------------------------------


@pytest.fixture
async def start_api(api: Api, database: Database) -> AsyncIterator[Api]:
    ticket_id = await _seed_ticket_worktree(api, database, name="modes")
    api.app.state.extra_test_ticket_id = ticket_id
    original = api.app.state.services
    store = AgentModeStore(api.settings.data_dir / AGENT_MODE_FILE)
    async with _manager(database, api.settings) as (manager, _deps):
        api.app.state.services = replace(original, runs=manager, agent_modes=store)
        try:
            yield api
        finally:
            api.app.state.services = original


async def _start(api: Api, **body: Any) -> Any:
    return await api.client.post(
        "/api/v1/runs",
        json={"ticket_id": api.app.state.extra_test_ticket_id, **body},
        headers=api.auth,
    )


async def test_a_run_stores_its_own_config(start_api: Api, database: Database) -> None:
    config = AgentModeConfig(
        mode=AgentMode.TIERED, lead_model="deepseek-v4-pro", worker_models=["deepseek-flash"]
    )

    response = await _start(start_api, agent_config=config.model_dump(mode="json"))

    assert response.status_code == 201
    assert response.json()["agent_config"] == config.model_dump(mode="json")
    workspace_id = start_api.app.state.services.principal.workspace_id
    async with database.sessions() as session:
        run = await ScopedRepo(session, Run, workspace_id).get(response.json()["id"])
    assert run is not None
    assert run.agent_config == config.model_dump(mode="json")


async def test_a_run_without_a_config_uses_the_saved_default(start_api: Api) -> None:
    saved = AgentModeConfig(mode=AgentMode.SINGLE, lead_model="deepseek-flash")
    start_api.app.state.services.agent_modes.write(saved)

    response = await _start(start_api)

    assert response.status_code == 201
    assert response.json()["agent_config"] == saved.model_dump(mode="json")


async def test_the_old_junior_runtime_override_still_works(start_api: Api) -> None:
    start_api.app.state.services.agent_modes.write(SINGLE)

    response = await _start(start_api, junior_runtime="claude_agent_sdk")

    assert response.status_code == 201
    assert response.json()["agent_config"]["worker_models"] == ["claude-opus-5-5"]


async def test_config_and_junior_runtime_together_are_422(start_api: Api) -> None:
    response = await _start(
        start_api,
        agent_config=SINGLE.model_dump(mode="json"),
        junior_runtime="native_deepseek",
    )

    assert response.status_code == 422


async def test_unknown_model_is_422(start_api: Api) -> None:
    response = await _start(start_api, agent_config={"mode": "single", "lead_model": "gpt-99"})

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "unknown_model"


async def test_a_provider_without_a_key_is_422_with_the_fix(start_api: Api) -> None:
    response = await _start(start_api, agent_config=CROWD.model_dump(mode="json"))

    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "provider_not_configured"
    assert "gaia secrets set OPENAI_API_KEY" in error["message"]
    assert "gaia secrets set XAI_API_KEY" in error["message"]


async def test_runs_start_without_a_store_use_the_settings_default(
    start_api: Api,
) -> None:
    services = start_api.app.state.services
    start_api.app.state.services = replace(services, agent_modes=None)

    response = await _start(start_api)

    assert response.status_code == 201
    assert response.json()["agent_config"]["mode"] == "tiered"


# --- planning --------------------------------------------------------------


class ScriptedLeadLlm:
    """Stands in for ``lead_llm``: one FakeLLM per model, or an error on entry."""

    def __init__(self, scripts: dict[str, Any]) -> None:
        self.scripts = scripts
        self.llms: dict[str, FakeLLM] = {}
        self.models: list[str] = []

    def __call__(self, **kwargs: Any) -> Any:
        return self._open(kwargs["model"])

    @asynccontextmanager
    async def _open(self, model: str) -> AsyncIterator[FakeLLM]:
        self.models.append(model)
        script = self.scripts[model]
        if isinstance(script, BaseException):
            raise script
        llm = FakeLLM([], parsed_responses=[_parsed(output) for output in script])
        self.llms[model] = llm
        yield llm


async def _fake_repo_context(root: Path, ticket: TicketInput, **kwargs: Any) -> RepoContext:
    return RepoContext(readme=None, tree="tree", files=(), omitted=())


async def _planner_for(
    database: Database,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    config: AgentModeConfig,
    scripts: dict[str, Any],
) -> tuple[GatewayPlanner, str, ScriptedLeadLlm]:
    async with session_scope(database.sessions) as session:
        chain = await make_chain(session, slug=f"plan-{config.mode.value}")
        await ScopedRepo(session, Run, chain.workspace.id).update(
            chain.run.id,
            worktree_id=chain.worktree.id,
            agent_config=config.model_dump(mode="json"),
        )
    fake = ScriptedLeadLlm(scripts)
    monkeypatch.setattr(planning, "lead_llm", fake)
    monkeypatch.setattr(planning, "build_repo_context", _fake_repo_context)
    planner = GatewayPlanner(
        settings=Settings(data_dir=tmp_path / "gaia-data"),
        sessions=database.sessions,
        price_table=PRICES,
    )
    return planner, chain.run.id, fake


async def test_straight_shot_plans_without_a_model_call(
    database: Database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    planner, run_id, fake = await _planner_for(database, tmp_path, monkeypatch, SINGLE, {})

    outcome = await planner.plan(run_id)

    assert outcome.skip_gate is True
    assert fake.models == []
    assert len(outcome.plan.steps) == 1
    assert "gpt-6.2-sol" in outcome.plan.summary


async def test_two_tiered_plans_on_the_lead_with_the_worker_roster(
    database: Database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = _plan(_step("easy", "gpt-6-luna"), _step("hard", "invented"))
    planner, run_id, fake = await _planner_for(
        database, tmp_path, monkeypatch, TIERED, {"claude-opus-5-5": [plan]}
    )

    outcome = await planner.plan(run_id)

    assert fake.models == ["claude-opus-5-5"]
    assert [step.worker for step in outcome.plan.steps] == ["gpt-6-luna", "deepseek-v4-pro"]
    message = fake.llms["claude-opus-5-5"].parse_calls[0].messages[0].content
    assert "## Workers" in message
    assert "`gpt-6-luna`: GPT-6 Luna (fast tier" in message
    assert outcome.skip_gate is False


async def test_crowd_merges_the_panel_and_notes_a_failed_panelist(
    database: Database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    merged = CrowdPlan(
        summary="Merged",
        risks=[],
        steps=[_step("merged")],
        provenance=["Step 1 comes from Plan A"],
    )
    planner, run_id, fake = await _planner_for(
        database,
        tmp_path,
        monkeypatch,
        CROWD,
        {
            "gpt-6.2-sol": [_plan(summary="from A")],
            "grok-4.7": RuntimeError("upstream down"),
            "deepseek-v4-pro": [_plan(summary="from C")],
            "claude-opus-5-5": [merged],
        },
    )

    outcome = await planner.plan(run_id)

    assert [candidate.label for candidate in outcome.candidates] == ["A", "C"]
    assert outcome.candidates[0].model == "gpt-6.2-sol"
    assert outcome.provenance == ("Step 1 comes from Plan A",)
    assert outcome.notes == ("Plan B (grok-4.7) failed: RuntimeError.",)
    assert outcome.plan.summary == "Merged"
    assert outcome.plan.steps[0].worker == "deepseek-v4-pro"
    synthesis = fake.llms["claude-opus-5-5"].parse_calls[0]
    assert synthesis.system == SYNTHESIZE_SYSTEM_PROMPT
    assert synthesis.output_model is CrowdPlan
    assert "Plan A" in synthesis.messages[0].content
    assert "from C" in synthesis.messages[0].content
    assert "grok" not in synthesis.messages[0].content
    meta = outcome.meta()
    assert meta["candidates"][1]["label"] == "C"


async def test_crowd_falls_back_to_the_lead_when_every_panelist_fails(
    database: Database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = AgentModeConfig(
        mode=AgentMode.CROWD,
        lead_model="claude-opus-5-5",
        worker_models=["deepseek-v4-pro"],
        panel_models=["gpt-6.2-sol", "grok-4.7"],
    )
    planner, run_id, _fake = await _planner_for(
        database,
        tmp_path,
        monkeypatch,
        config,
        {
            "gpt-6.2-sol": RuntimeError("a"),
            "grok-4.7": ValueError("b"),
            "claude-opus-5-5": [_plan(summary="solo")],
        },
    )

    outcome = await planner.plan(run_id)

    assert outcome.plan.summary == "solo"
    assert outcome.candidates == ()
    assert outcome.notes[-1] == "No panelist produced a plan, so the lead planned alone."


@pytest.mark.parametrize("error", [LLMBudgetExceeded(), asyncio.CancelledError()])
async def test_a_panel_budget_stop_or_cancel_is_not_swallowed(
    database: Database,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    error: BaseException,
) -> None:
    planner, run_id, _fake = await _planner_for(
        database,
        tmp_path,
        monkeypatch,
        CROWD,
        {"gpt-6.2-sol": [_plan()], "grok-4.7": error, "deepseek-v4-pro": [_plan()]},
    )

    with pytest.raises(type(error)):
        await planner.plan(run_id)


def test_straight_shot_plan_fits_the_plan_limits() -> None:
    ticket = TicketInput(id="t", title="  ", body="x" * 30_000)

    plan = straight_shot_plan(ticket, "m")

    assert plan.steps[0].title == "Do the ticket"
    assert len(plan.steps[0].instructions) == 20_000
    assert plan.steps[0].test_command is None


def test_workers_note_handles_unpriced_and_untiered_models() -> None:
    config = AgentModeConfig(
        mode=AgentMode.TIERED, lead_model="lead", worker_models=["plain", "unlisted"]
    )
    prices = PriceTable(
        {"plain": ModelPrice(rates=PriceRates(input=Decimal(1), output=Decimal(2)))}
    )

    note = workers_note(config, prices)

    assert "- `plain`: plain ($1 in / $2 out per 1M tokens)" in note
    assert note.endswith("- `unlisted`")


def test_assign_workers_keeps_the_rest_of_the_plan() -> None:
    plan = _plan(_step("a", "gpt-6-luna"), _step("b"))

    assigned = assign_workers(plan, TIERED)

    assert [step.worker for step in assigned.steps] == ["gpt-6-luna", "deepseek-v4-pro"]
    assert assigned.summary == plan.summary


async def test_synthesis_retries_a_denied_test_command_once() -> None:
    bad = CrowdPlan(
        summary="s",
        risks=[],
        steps=[_step().model_copy(update={"test_command": ["bash", "-c", "x"]})],
        provenance=["p"],
    )
    good = CrowdPlan(summary="s", risks=[], steps=[_step()], provenance=["p"])
    llm = FakeLLM([], parsed_responses=[_parsed(bad), _parsed(good)])
    engineer = LeadEngineer(llm, CommandPolicy.from_default())

    result = await engineer.synthesize_plan(
        TicketInput(id="t", title="T", body="B"),
        RepoContext(readme=None, tree="tree", files=(), omitted=()),
        [("A", _plan())],
    )

    assert result == good
    assert "valid CrowdPlan" in llm.parse_calls[1].messages[-1].content
    assert result.as_plan() == Plan(summary="s", risks=[], steps=[_step()])


# --- review ----------------------------------------------------------------


async def test_straight_shot_skips_the_final_review_and_reviews_on_its_model(
    database: Database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with session_scope(database.sessions) as session:
        chain = await make_chain(session, slug="review-single")
        await ScopedRepo(session, Run, chain.workspace.id).update(
            chain.run.id,
            worktree_id=chain.worktree.id,
            agent_config=SINGLE.model_dump(mode="json"),
        )
        await ScopedRepo(session, RunStep, chain.workspace.id).update(
            chain.step.id, spec_json=_step().model_dump()
        )
    seen: list[str | None] = []

    @asynccontextmanager
    async def fake_lead_llm(**kwargs: Any) -> AsyncIterator[FakeLLM]:
        seen.append(kwargs["model"])
        yield FakeLLM([], parsed_responses=[_parsed(STRAIGHT_SHOT_FINAL_REVIEW)])

    monkeypatch.setattr(review_module, "lead_llm", fake_lead_llm)
    reviewer = GatewayReviewer(
        settings=Settings(data_dir=tmp_path / "gaia-data"),
        sessions=database.sessions,
        price_table=PRICES,
        git=cast(GitService, object()),
    )

    final = await reviewer.final_review(chain.run.id)
    step = await reviewer.review_step(
        chain.run.id,
        0,
        cast(Any, type("Outcome", (), {"diff": "d"})()),
        cast(Any, type("Checks", (), {"output": "ok"})()),
    )

    assert final == STRAIGHT_SHOT_FINAL_REVIEW
    assert step.verdict == "accept"
    assert seen == ["gpt-6.2-sol"]


# --- graph nodes -----------------------------------------------------------


def _state(run_id: str, workspace_id: str, **extra: Any) -> Any:
    return {"run_id": run_id, "workspace_id": workspace_id, "step_index": 0, **extra}


async def test_lead_plan_keeps_the_outcome_meta_and_approve_plan_skips_the_gate(
    database: Database, tmp_path: Path
) -> None:
    async with session_scope(database.sessions) as session:
        chain = await make_chain(session, slug="nodes-single")
    deps = cast(
        Any,
        type(
            "Deps",
            (),
            {
                "planner": FakePlanner(outcome=PlanOutcome(plan=make_plan(1), skip_gate=True)),
                "sessions": database.sessions,
            },
        )(),
    )
    state = _state(chain.run.id, chain.workspace.id)

    planned = await lead_plan(state, deps)
    routed = await approve_plan(cast(Any, {**state, **planned}), deps)

    assert planned["plan_meta"]["skip_gate"] is True
    assert routed == {"route": "implement_step"}


async def test_approve_plan_puts_the_crowd_on_the_approval(
    database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    from gaia.orchestrator.nodes import approve_plan as approve_module

    async with session_scope(database.sessions) as session:
        chain = await make_chain(session, slug="nodes-crowd")
        await ScopedRepo(session, Run, chain.workspace.id).update(
            chain.run.id, status=RunStatus.PLANNING.value
        )
    outcome = PlanOutcome(
        plan=make_plan(1),
        candidates=(PlanCandidate(label="A", model="gpt-6.2-sol", plan=make_plan(1)),),
        provenance=("from A",),
    )
    captured: dict[str, Any] = {}

    def fake_interrupt(payload: dict[str, Any]) -> dict[str, Any]:
        captured.update(payload)
        raise RuntimeError("stop here")

    published: list[Any] = []

    class Events:
        async def publish(self, **kwargs: Any) -> None:
            published.append(kwargs)

    deps = cast(Any, type("Deps", (), {"sessions": database.sessions, "events": Events()})())
    state = _state(
        chain.run.id,
        chain.workspace.id,
        plan=outcome.plan.model_dump(),
        plan_meta=outcome.meta(),
    )
    monkeypatch.setattr(approve_module, "interrupt", fake_interrupt)
    with pytest.raises(RuntimeError, match="stop here"):
        await approve_plan(state, deps)

    assert captured["candidates"][0]["model"] == "gpt-6.2-sol"
    assert captured["provenance"] == ["from A"]
    assert "notes" not in captured
