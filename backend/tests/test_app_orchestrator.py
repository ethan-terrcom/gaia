"""Lifespan wiring for the orchestrator (12b-3)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from gaia.api import app as app_module
from gaia.api.app import create_app
from gaia.config.secrets import InMemorySecretStore, SecretName
from gaia.config.settings import Settings
from gaia.orchestrator.runner import RunManager
from gaia.orchestrator.wiring import Wiring
from gaia.runtimes.approvals import DbApprovalBroker
from gaia.runtimes.orphans import OrphanRegistry


async def test_lifespan_builds_and_closes_the_run_manager(
    database: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = InMemorySecretStore()
    store.set(SecretName.GAIA_API_TOKEN, "admin-token")
    monkeypatch.setattr(app_module, "get_secret_store", lambda: store)

    settings = Settings(
        data_dir=tmp_path / "gaia-data",
        db_url=database.url,
        orchestrator_enabled=True,
        pr_sync_enabled=False,
    )

    closed = False

    class RecordingRunManager(RunManager):
        async def aclose(self) -> None:
            nonlocal closed
            closed = True
            await super().aclose()

    monkeypatch.setattr(app_module, "RunManager", RecordingRunManager)
    app = create_app(settings)

    async with app.router.lifespan_context(app):
        services = app.state.services
        assert isinstance(services.runs, RunManager)
        assert isinstance(services.runs, RecordingRunManager)
        assert isinstance(services.approvals, DbApprovalBroker)
        assert isinstance(services.wiring, Wiring)

    assert closed


async def test_lifespan_starts_when_recover_fails(
    database: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = InMemorySecretStore()
    store.set(SecretName.GAIA_API_TOKEN, "admin-token")
    monkeypatch.setattr(app_module, "get_secret_store", lambda: store)

    settings = Settings(
        data_dir=tmp_path / "gaia-data",
        db_url=database.url,
        orchestrator_enabled=True,
        pr_sync_enabled=False,
    )

    class RecoverFailingRunManager(RunManager):
        async def recover(self) -> None:
            raise RuntimeError("recover boom")

    monkeypatch.setattr(app_module, "RunManager", RecoverFailingRunManager)
    app = create_app(settings)

    async with app.router.lifespan_context(app):
        services = app.state.services
        assert isinstance(services.runs, RecoverFailingRunManager)
        assert isinstance(services.approvals, DbApprovalBroker)
        assert isinstance(services.wiring, Wiring)


async def test_lifespan_with_orchestrator_disabled_leaves_runs_none(
    database: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = InMemorySecretStore()
    store.set(SecretName.GAIA_API_TOKEN, "admin-token")
    monkeypatch.setattr(app_module, "get_secret_store", lambda: store)

    settings = Settings(
        data_dir=tmp_path / "gaia-data",
        db_url=database.url,
        orchestrator_enabled=False,
        pr_sync_enabled=False,
    )
    app = create_app(settings)

    async with app.router.lifespan_context(app):
        services = app.state.services
        assert services.runs is None
        assert services.approvals is None
        assert services.wiring is None


async def test_lifespan_reaps_orphans_before_recovering_and_survives_a_failure(
    database: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Lead review W2: requirement 9's startup reap runs before any run resumes,
    and a failing reap is logged, not fatal."""
    store = InMemorySecretStore()
    store.set(SecretName.GAIA_API_TOKEN, "admin-token")
    monkeypatch.setattr(app_module, "get_secret_store", lambda: store)
    order: list[str] = []

    async def reap(self: Any, *, is_ours: Any) -> None:
        order.append("reap")
        raise RuntimeError("reap boom")

    class RecordingRunManager(RunManager):
        async def recover(self) -> None:
            order.append("recover")

    monkeypatch.setattr(OrphanRegistry, "reap_stale", reap)
    monkeypatch.setattr(app_module, "RunManager", RecordingRunManager)
    settings = Settings(
        data_dir=tmp_path / "gaia-data",
        db_url=database.url,
        orchestrator_enabled=True,
        pr_sync_enabled=False,
        command_approval_timeout_s=120,
    )
    app = create_app(settings)

    async with app.router.lifespan_context(app):
        wiring = app.state.services.wiring
        assert wiring.runtimes.get("claude_agent_sdk")._approval_timeout_s == 120

    assert order == ["reap", "recover"]
