"""Demo mode (18b-2): the gateway is fake, but everything else is real.

These tests prove the two security properties first: demo mode never reads a
real provider key, and the fake transport never touches the network. Then they
drive a real run from ``POST /runs`` to the ``pr_open`` approval.
"""

from __future__ import annotations

import asyncio
import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import httpx2
import pytest

from gaia.api import app as app_module
from gaia.api.app import create_app
from gaia.config.secrets import InMemorySecretStore, SecretName
from gaia.config.settings import Settings
from gaia.db.engine import session_scope
from gaia.db.models import Approval, Repository, Run, Ticket, Worktree
from gaia.db.models import LlmUsage as LlmUsageRow
from gaia.db.repo import ScopedRepo
from gaia.domain.enums import ApprovalKind, RunStatus
from gaia.llm import run_tokens
from gaia.orchestrator.impl import worktree_location
from gaia.orchestrator.wiring import build_wiring as original_build_wiring
from gaia.services.git import GitService
from tests.conftest import API_BASE_URL
from tests.db_builders import make_chain
from tests.orchestrator_fakes import FakePullRequests
from tests.tool_fakes import FakeCommandRunner


class _ReusableAsyncClient(httpx2.AsyncClient):
    """An httpx2 client whose SDK close is a no-op until the test is done.

    The Anthropic/OpenAI SDKs close the supplied http client after each per-call
    lead/native client is closed. The wiring passes one shared ASGI client, so
    the shared client must stay open until the test closes it explicitly.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._really_close = False

    async def aclose(self) -> None:
        if self._really_close:
            await super().aclose()

    async def close_for_real(self) -> None:
        self._really_close = True
        await self.aclose()


async def _wait_for_task(manager: Any, run_id: str, *, timeout_s: float = 120.0) -> None:
    """Wait for the current graph task for ``run_id`` to finish."""
    async with asyncio.timeout(timeout_s):
        await manager._tasks[run_id]


async def test_gateway_demo_mode_is_metered_and_never_reads_provider_keys(
    tmp_path: Path,
    database: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = InMemorySecretStore()
    store.set(SecretName.GAIA_API_TOKEN, "admin-token")
    monkeypatch.setattr(app_module, "get_secret_store", lambda: store)

    original_require = store.require

    def require_only_api_token(name: SecretName) -> Any:
        assert name is SecretName.GAIA_API_TOKEN, f"demo mode read {name}"
        return original_require(name)

    monkeypatch.setattr(store, "require", require_only_api_token)

    settings = Settings(
        data_dir=tmp_path / "gaia-data",
        db_url=database.url,
        fake_llm=True,
        orchestrator_enabled=False,
        pr_sync_enabled=False,
        cleanup_enabled=False,
        anthropic_base_url="https://anthropic.test",
        junior_base_url="https://deepseek.test",
    )
    app = create_app(settings)

    async with app.router.lifespan_context(app):
        async with session_scope(database.sessions) as session:
            chain = await make_chain(session, slug="demo-gateway")
            runs = ScopedRepo(session, Run, chain.workspace.id)
            await runs.update(chain.run.id, budget_usd=Decimal("5.0"))
            minted = await run_tokens.mint(
                session,
                workspace_id=chain.workspace.id,
                run_id=chain.run.id,
                run_step_id=chain.step.id,
                agent_id=chain.agent.id,
                provider="deepseek",
                ttl_s=3600,
            )

        token = minted.token.get_secret_value()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url=API_BASE_URL
        ) as client:
            response = await client.post(
                "/llm/deepseek/chat/completions",
                content=json.dumps({"model": "deepseek-v4-pro"}),
                headers={"Authorization": f"Bearer {token}"},
            )
            api_token = store.require(SecretName.GAIA_API_TOKEN).get_secret_value()
            me = await client.get(
                "/api/v1/me",
                headers={"Authorization": f"Bearer {api_token}"},
            )

    assert response.status_code == 200
    assert me.status_code == 200
    assert me.json()["demo_mode"] is True

    async with session_scope(database.sessions) as session:
        usage = await ScopedRepo(session, LlmUsageRow, chain.workspace.id).list()
        assert len(usage) == 1
        assert usage[0].provider == "deepseek"
        assert usage[0].cost_usd > 0
        run = await ScopedRepo(session, Run, chain.workspace.id).get(chain.run.id)
        assert run is not None
        assert run.cost_usd == usage[0].cost_usd


async def test_demo_mode_e2e_from_post_runs_to_pr_open(
    database: Any,
    git_repo: Path,
    git_service: GitService,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = InMemorySecretStore()
    store.set(SecretName.GAIA_API_TOKEN, "admin-token")
    monkeypatch.setattr(app_module, "get_secret_store", lambda: store)

    settings = Settings(
        data_dir=tmp_path / "gaia-data",
        db_url=database.url,
        fake_llm=True,
        orchestrator_enabled=True,
        pr_sync_enabled=False,
        cleanup_enabled=False,
        run_budget_usd=5.0,
        anthropic_base_url="https://anthropic.test",
        junior_base_url="https://deepseek.test",
        step_timeout_s=120,
        check_timeout_s=120,
        command_approval_timeout_s=60,
    )

    shared_clients: list[_ReusableAsyncClient] = []
    app_ref: Any = None

    def build_wiring_for_test(**kwargs: Any) -> Any:
        assert app_ref is not None
        client = _ReusableAsyncClient(transport=httpx2.ASGITransport(app=app_ref))
        shared_clients.append(client)
        return original_build_wiring(
            **kwargs,
            llm_http_client=client,
            command_runner=FakeCommandRunner(),
            pull_requests=FakePullRequests(pr_number=1, default_ci="passed"),
        )

    monkeypatch.setattr(app_module, "build_wiring", build_wiring_for_test)
    app = create_app(settings)
    app_ref = app

    async with app.router.lifespan_context(app):
        services = app.state.services
        workspace_id = services.principal.workspace_id
        user_id = services.principal.user_id

        async with session_scope(services.sessions) as session:
            repositories = ScopedRepo(session, Repository, workspace_id)
            repository = await repositories.create(
                name="demo-e2e",
                local_path=str(git_repo),
                setup_command=None,
                test_command=["python3", "-m", "unittest", "-q"],
                created_by=user_id,
            )
            tickets = ScopedRepo(session, Ticket, workspace_id)
            ticket = await tickets.create(
                repo_id=repository.id,
                title="Add a demo note",
                body="Write GAIA_DEMO.md with a short note.",
                created_by=user_id,
            )

        auth = {
            "Authorization": (
                f"Bearer {store.require(SecretName.GAIA_API_TOKEN).get_secret_value()}"
            )
        }
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url=API_BASE_URL
        ) as client:
            refused = await client.post(
                "/api/v1/runs",
                json={"ticket_id": ticket.id, "junior_runtime": "claude_agent_sdk"},
                headers=auth,
            )
            assert refused.status_code == 422
            assert "GAIA_FAKE_LLM" in refused.json()["error"]["message"]

            started = await client.post(
                "/api/v1/runs",
                json={"ticket_id": ticket.id},
                headers=auth,
            )
            assert started.status_code == 201
            run_id = started.json()["id"]

        # Plan approval.
        await _wait_for_task(services.runs, run_id)
        await services.runs.resume(run_id, {"action": "approve"})
        # Implementation, checks, review, commit, final review → pr_open gate.
        await _wait_for_task(services.runs, run_id)

        async with session_scope(services.sessions) as session:
            run = await ScopedRepo(session, Run, workspace_id).get(run_id)
            assert run is not None
            assert run.status == RunStatus.AWAITING_APPROVAL.value
            assert run.worktree_id is not None

            approvals = await ScopedRepo(session, Approval, workspace_id).list(run_id=run_id)
            assert any(
                approval.kind == ApprovalKind.PR_OPEN.value and approval.status == "pending"
                for approval in approvals
            )

            worktree = await ScopedRepo(session, Worktree, workspace_id).get(run.worktree_id)
            assert worktree is not None
            assert worktree.base_sha is not None

        repo_path = await git_service.validate_repo(repository.local_path)
        location = await worktree_location(
            git=git_service,
            repo_path=repo_path,
            worktree=worktree,
        )
        cwd, env = git_service._command_context(location)
        tree = await git_service._run("ls-tree", "-r", "--name-only", "HEAD", cwd=cwd, env=env)
        assert "GAIA_DEMO.md" in tree.stdout

    for shared_client in shared_clients:
        await shared_client.close_for_real()


# --- agent modes end to end (doc 19b) --------------------------------------

STRAIGHT_SHOT = {"mode": "single", "lead_model": "gpt-6.2-sol"}
CROWD = {
    "mode": "crowd",
    "lead_model": "claude-opus-5-5",
    "worker_models": ["deepseek-v4-pro"],
    "panel_models": ["gpt-6.2-sol", "grok-4.7", "deepseek-flash"],
}


async def _demo_run(
    database: Any,
    git_repo: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    agent_config: dict[str, Any],
) -> Any:
    """Build a demo-mode app whose LLM transport is the in-process fake.

    Returns ``(app, store, shared_clients)``; ``agent_config`` is the caller's.
    """
    del agent_config
    store = InMemorySecretStore()
    store.set(SecretName.GAIA_API_TOKEN, "admin-token")
    monkeypatch.setattr(app_module, "get_secret_store", lambda: store)
    settings = Settings(
        data_dir=tmp_path / "gaia-data",
        db_url=database.url,
        fake_llm=True,
        orchestrator_enabled=True,
        pr_sync_enabled=False,
        cleanup_enabled=False,
        run_budget_usd=5.0,
        step_timeout_s=120,
        check_timeout_s=120,
        command_approval_timeout_s=60,
    )
    shared: list[_ReusableAsyncClient] = []
    app_ref: Any = None

    def build_wiring_for_test(**kwargs: Any) -> Any:
        client = _ReusableAsyncClient(transport=httpx2.ASGITransport(app=app_ref))
        shared.append(client)
        return original_build_wiring(
            **kwargs,
            llm_http_client=client,
            command_runner=FakeCommandRunner(),
            pull_requests=FakePullRequests(pr_number=1, default_ci="passed"),
        )

    monkeypatch.setattr(app_module, "build_wiring", build_wiring_for_test)
    app = create_app(settings)
    app_ref = app
    return app, store, shared


@pytest.mark.parametrize(
    ("agent_config", "plan_gate"),
    [(STRAIGHT_SHOT, False), (CROWD, True)],
    ids=["straight-shot", "crowd"],
)
async def test_demo_mode_runs_each_agent_mode(
    database: Any,
    git_repo: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    agent_config: dict[str, Any],
    plan_gate: bool,
) -> None:
    app, store, shared = await _demo_run(database, git_repo, tmp_path, monkeypatch, agent_config)

    async with app.router.lifespan_context(app):
        services = app.state.services
        workspace_id = services.principal.workspace_id
        async with session_scope(services.sessions) as session:
            repository = await ScopedRepo(session, Repository, workspace_id).create(
                name="modes-e2e",
                local_path=str(git_repo),
                test_command=["python3", "-m", "unittest", "-q"],
            )
            ticket = await ScopedRepo(session, Ticket, workspace_id).create(
                repo_id=repository.id,
                title="Add a demo note",
                body="Write GAIA_DEMO.md with a short note.",
            )
        auth = {
            "Authorization": (
                f"Bearer {store.require(SecretName.GAIA_API_TOKEN).get_secret_value()}"
            )
        }
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url=API_BASE_URL
        ) as client:
            started = await client.post(
                "/api/v1/runs",
                json={"ticket_id": ticket.id, "agent_config": agent_config},
                headers=auth,
            )
        assert started.status_code == 201
        run_id = started.json()["id"]
        await _wait_for_task(services.runs, run_id)

        async with session_scope(services.sessions) as session:
            approvals = await ScopedRepo(session, Approval, workspace_id).list(run_id=run_id)
            usage = await ScopedRepo(session, LlmUsageRow, workspace_id).list(run_id=run_id)
        kinds = [approval.kind for approval in approvals]
        providers = {row.provider for row in usage}

        if plan_gate:
            assert kinds == [ApprovalKind.PLAN.value]
            payload = approvals[0].payload_json
            assert [entry["label"] for entry in payload["candidates"]] == ["A", "B", "C"]
            assert payload["provenance"] == ["Demo: the one step comes from Plan A."]
            assert payload["plan"]["steps"][0]["worker"] == "deepseek-v4-pro"
            assert providers == {"anthropic", "openai", "xai", "deepseek"}
        else:
            # No plan gate: the run went straight to work and stopped at the PR.
            assert kinds == [ApprovalKind.PR_OPEN.value]
            assert providers == {"openai"}
            assert {row.model for row in usage} == {"gpt-6.2-sol"}

    for client_ in shared:
        await client_.close_for_real()
