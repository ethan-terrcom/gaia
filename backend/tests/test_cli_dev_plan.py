"""Tests for `gaia dev plan` (doc 11 acceptance criterion)."""

from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from gaia import cli
from gaia.config.secrets import InMemorySecretStore, SecretName
from gaia.config.settings import Settings
from gaia.db.models import Agent, Repository, Run, Ticket
from gaia.db.models import LlmUsage as LlmUsageRow
from gaia.db.repo import ScopedRepo
from gaia.db.seed import seed_initial_data
from gaia.domain.enums import RunStatus
from gaia.domain.plans import Plan, PlanStep
from gaia.llm.anthropic_client import AnthropicClient
from gaia.llm.base import LLMUsage, ParsedResponse
from gaia.llm.fake import FakeLLM
from gaia.orchestrator.lead import PlanRejected
from tests.conftest import Database

DUMMY_KEY = "sk-ant-test-1234567890"


def _plan() -> Plan:
    return Plan(
        summary="Add a test",
        risks=[],
        steps=[
            PlanStep(
                title="Write a test",
                instructions="Add a test.",
                files_likely_touched=["tests/test_x.py"],
                acceptance=["pytest passes"],
                test_command=None,
            )
        ],
    )


def _parsed_plan() -> ParsedResponse[Plan]:
    return ParsedResponse(
        output=_plan(),
        usage=LLMUsage(provider="fake", model="fake-model", input_tokens=10, output_tokens=5),
        model="fake-model",
        request_id=None,
        stop_reason="end_turn",
    )


@pytest.fixture
def settings(tmp_path: Path, database: Database) -> Settings:
    return Settings(data_dir=tmp_path / "gaia-data", db_url=database.url)


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch) -> InMemorySecretStore:
    fake = InMemorySecretStore()
    fake.set(SecretName.ANTHROPIC_API_KEY, DUMMY_KEY)
    monkeypatch.setattr(cli, "get_secret_store", lambda: fake)
    return fake


async def _make_repo_and_ticket(
    database: Database,
    settings: Settings,
    tmp_path: Path,
    *,
    test_command: list[str] | None = None,
) -> tuple[str, str]:
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    (repo_dir / "README.md").write_text("hello", encoding="utf-8")
    (repo_dir / "src").mkdir()
    (repo_dir / "src" / "app.py").write_text("x = 1", encoding="utf-8")

    async with database.sessions() as session:
        seeded = await seed_initial_data(session, settings)
        repositories = ScopedRepo(session, Repository, seeded.workspace.id)
        repository = await repositories.create(
            name="repo",
            local_path=str(repo_dir),
            test_command=test_command,
            created_by=seeded.user.id,
        )
        tickets = ScopedRepo(session, Ticket, seeded.workspace.id)
        ticket = await tickets.create(
            repo_id=repository.id,
            title="Add a test",
            body="Change src/app.py and add a test.",
            created_by=seeded.user.id,
        )
        await session.commit()
    return seeded.workspace.id, ticket.id


async def test_dev_plan_with_fake_llm_creates_run_records_usage_and_cancels(
    database: Database,
    settings: Settings,
    store: InMemorySecretStore,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(cli, "get_settings", lambda: settings)
    workspace_id, ticket_id = await _make_repo_and_ticket(
        database, settings, tmp_path, test_command=["uv", "run", "pytest"]
    )

    async def fake_make_client(settings: object, store: object, recorder: object) -> FakeLLM:
        return FakeLLM([], parsed_responses=[_parsed_plan()], recorder=recorder)  # type: ignore[arg-type]

    monkeypatch.setattr(cli, "_make_client", fake_make_client)

    assert await cli._dev_plan_async(argparse.Namespace(ticket_id=ticket_id)) == 0

    captured = capsys.readouterr()
    assert '"summary": "Add a test"' in captured.out
    assert "cost: $0.0000 (10 prompt / 5 output tokens)" in captured.out

    async with database.sessions() as session:
        runs = ScopedRepo(session, Run, workspace_id)
        run_rows = await runs.list(ticket_id=ticket_id)
        assert len(run_rows) == 1
        run = run_rows[0]
        assert run.status == RunStatus.CANCELLED.value
        assert run.ticket_id == ticket_id
        usage_rows = await ScopedRepo(session, LlmUsageRow, workspace_id).list(run_id=run.id)
        assert len(usage_rows) == 1
        assert usage_rows[0].input_tokens == 10
        assert usage_rows[0].output_tokens == 5


async def test_dev_plan_without_registered_test_command(
    database: Database,
    settings: Settings,
    store: InMemorySecretStore,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(cli, "get_settings", lambda: settings)
    workspace_id, ticket_id = await _make_repo_and_ticket(
        database, settings, tmp_path, test_command=None
    )

    async def fake_make_client(settings: object, store: object, recorder: object) -> FakeLLM:
        return FakeLLM([], parsed_responses=[_parsed_plan()], recorder=recorder)  # type: ignore[arg-type]

    monkeypatch.setattr(cli, "_make_client", fake_make_client)

    assert await cli._dev_plan_async(argparse.Namespace(ticket_id=ticket_id)) == 0

    captured = capsys.readouterr()
    assert "cost: $0.0000" in captured.out
    assert workspace_id


async def test_dev_plan_missing_ticket_returns_1(
    database: Database,
    settings: Settings,
    store: InMemorySecretStore,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(cli, "get_settings", lambda: settings)

    assert await cli._dev_plan_async(argparse.Namespace(ticket_id="missing")) == 1

    captured = capsys.readouterr()
    assert "no ticket 'missing'" in captured.err


async def test_dev_plan_missing_repository_returns_1(
    database: Database,
    settings: Settings,
    store: InMemorySecretStore,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(cli, "get_settings", lambda: settings)
    workspace_id, ticket_id = await _make_repo_and_ticket(database, settings, tmp_path)

    original_get = ScopedRepo.get

    async def fake_get(self: object, row_id: str) -> object | None:
        if getattr(self, "model", None) is Repository:
            return None
        return await original_get(self, row_id)  # type: ignore[arg-type,func-returns-value]

    monkeypatch.setattr(ScopedRepo, "get", fake_get)

    assert await cli._dev_plan_async(argparse.Namespace(ticket_id=ticket_id)) == 1

    captured = capsys.readouterr()
    assert "has no repository" in captured.err
    assert workspace_id


async def test_dev_plan_missing_lead_agent_returns_1(
    database: Database,
    settings: Settings,
    store: InMemorySecretStore,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(cli, "get_settings", lambda: settings)
    workspace_id, ticket_id = await _make_repo_and_ticket(database, settings, tmp_path)

    original_list = ScopedRepo.list

    async def fake_list(self: object, **filters: object) -> list[object]:
        if getattr(self, "model", None) is Agent and filters.get("display_name") == "lead":
            return []
        return await original_list(self, **filters)  # type: ignore[arg-type]

    monkeypatch.setattr(ScopedRepo, "list", fake_list)

    assert await cli._dev_plan_async(argparse.Namespace(ticket_id=ticket_id)) == 1

    captured = capsys.readouterr()
    assert "seeded lead agent is missing" in captured.err
    assert workspace_id


async def test_dev_plan_reports_missing_anthropic_key(
    database: Database,
    settings: Settings,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(cli, "get_settings", lambda: settings)
    monkeypatch.setattr(cli, "get_secret_store", lambda: InMemorySecretStore())
    workspace_id, ticket_id = await _make_repo_and_ticket(database, settings, tmp_path)

    assert await cli._dev_plan_async(argparse.Namespace(ticket_id=ticket_id)) == 1

    captured = capsys.readouterr()
    assert "ANTHROPIC_API_KEY is not set" in captured.err
    assert workspace_id


async def test_dev_plan_reports_plan_rejection(
    database: Database,
    settings: Settings,
    store: InMemorySecretStore,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(cli, "get_settings", lambda: settings)
    workspace_id, ticket_id = await _make_repo_and_ticket(database, settings, tmp_path)

    class _RejectingLLM:
        async def parse(self, **kwargs: object) -> ParsedResponse[Plan]:
            raise PlanRejected(["denied: git"])

    async def fake_make_client(settings: object, store: object, recorder: object) -> _RejectingLLM:
        return _RejectingLLM()

    monkeypatch.setattr(cli, "_make_client", fake_make_client)

    assert await cli._dev_plan_async(argparse.Namespace(ticket_id=ticket_id)) == 1

    captured = capsys.readouterr()
    assert "lead plan rejected" in captured.err
    assert workspace_id


async def test_make_client_constructs_real_client(
    settings: Settings, store: InMemorySecretStore
) -> None:
    recorder = _RecordingRecorder()

    client = await cli._make_client(settings, store, recorder)

    assert isinstance(client, AnthropicClient)


async def test_make_client_requires_lead_model(
    store: InMemorySecretStore,
) -> None:
    recorder = _RecordingRecorder()

    with pytest.raises(ValueError, match="lead_model"):
        await cli._make_client(object(), store, recorder)


def test_dev_plan_runs_async_body(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_async_body(args: argparse.Namespace) -> int:
        assert args.ticket_id == "ticket-123"
        return 7

    monkeypatch.setattr(cli, "_dev_plan_async", fake_async_body)

    assert cli._dev_plan(argparse.Namespace(ticket_id="ticket-123")) == 7


class _RecordingRecorder:
    def __init__(self) -> None:
        self.records: list[object] = []

    async def record(self, usage: object, *, cost_usd: object, price_tier: object = None) -> None:
        self.records.append((usage, cost_usd, price_tier))


def test_dev_plan_parser_and_routing(monkeypatch: pytest.MonkeyPatch) -> None:
    parser = cli._build_parser()
    args = parser.parse_args(["dev", "plan", "ticket-123"])

    assert args.dev_command == "plan"
    assert args.ticket_id == "ticket-123"

    calls: list[str] = []

    def fake_dev_plan(args: argparse.Namespace) -> int:
        calls.append(args.ticket_id)
        return 0

    monkeypatch.setattr(cli, "_dev_plan", fake_dev_plan)

    assert cli._dev(argparse.Namespace(dev_command="plan", ticket_id="ticket-123")) == 0
    assert calls == ["ticket-123"]
