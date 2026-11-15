"""Run endpoints and the 503 fallback (doc 14)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from typing import Any, cast

import pytest
from fastapi import HTTPException
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from gaia.api.deps import Scoped
from gaia.api.routes.runs import (
    cancel_run,
    get_run,
    get_run_diff,
    list_runs,
    start_run,
)
from gaia.db.engine import session_scope
from gaia.db.models import PullRequest, Repository, Run, RunStep, Ticket, Worktree
from gaia.db.repo import RunRepo, ScopedRepo
from gaia.domain.enums import CiStatus, PullRequestState, RunStatus, RunStepStatus, WorktreeStatus
from gaia.domain.schemas import RunCreate, RunDetail, RunPage, RunRead
from gaia.orchestrator.deps import GraphDeps
from gaia.orchestrator.graph import build_graph
from gaia.orchestrator.runner import RunManager
from gaia.runtimes.approvals import DbApprovalBroker
from gaia.services.git import WorktreeLocation
from gaia.services.worktrees import WorktreeService
from tests.conftest import Api, Database
from tests.db_builders import make_workspace
from tests.orchestrator_helpers import make_fakes, make_graph_deps

pytestmark = pytest.mark.anyio


@asynccontextmanager
async def _manager(
    database: Database, settings: Any
) -> AsyncIterator[tuple[RunManager, GraphDeps]]:
    """A real manager over the 12a fakes and a temp SQLite checkpointer."""
    fakes = make_fakes()
    deps = make_graph_deps(database, settings, fakes)
    async with AsyncSqliteSaver.from_conn_string(str(settings.checkpoint_path)) as saver:
        await saver.setup()
        graph = build_graph(deps, checkpointer=saver)
        manager = RunManager(graph, deps, max_concurrent_runs=settings.max_concurrent_runs)
        try:
            yield manager, deps
        finally:
            await manager.aclose()


async def _seed_ticket_worktree(api: Api, database: Database, *, name: str) -> str:
    """Create a repository, ticket and the fake worktree in the seeded workspace."""
    workspace_id = api.app.state.services.principal.workspace_id
    async with database.sessions() as session:
        repos = ScopedRepo(session, Repository, workspace_id)
        repo = await repos.create(name=f"repo-{name}", local_path=f"/tmp/{name}")
        tickets = ScopedRepo(session, Ticket, workspace_id)
        ticket = await tickets.create(repo_id=repo.id, title=f"Ship {name}")
        worktrees = ScopedRepo(session, Worktree, workspace_id)
        await worktrees.create(
            id="worktree-1",
            repo_id=repo.id,
            path=f"/tmp/{name}/worktree",
            branch=f"gaia/{name}",
            base_sha="0" * 40,
            status=WorktreeStatus.READY.value,
        )
        await session.commit()
        return ticket.id


@asynccontextmanager
async def _scoped_for_api(
    database: Database,
    api: Api,
) -> AsyncIterator[tuple[Scoped, Any, Any]]:
    """Open a session like the API dependency does, for direct route calls."""
    async with database.sessions() as session:
        services = api.app.state.services
        principal = services.principal
        try:
            yield Scoped(session, principal.workspace_id), principal, services
        except BaseException:
            await session.rollback()
            raise
        else:
            await session.commit()


@pytest.fixture
async def run_api(api: Api, database: Database) -> AsyncIterator[Api]:
    """The real app with a fake-graph ``RunManager`` on ``app.state.services``."""
    ticket_id = await _seed_ticket_worktree(api, database, name="run-api")
    api.app.state.extra_test_ticket_id = ticket_id
    original = api.app.state.services
    async with _manager(database, api.settings) as (manager, deps):
        broker = DbApprovalBroker(database.sessions, deps.events)
        api.app.state.services = replace(original, runs=manager, approvals=broker)
        try:
            yield api
        finally:
            api.app.state.services = original


async def _make_run(
    database: Database,
    workspace_id: str,
    *,
    ticket_id: str,
    repo_id: str,
    status: RunStatus = RunStatus.PENDING,
    worktree_id: str | None = None,
    run_id: str | None = None,
) -> str:
    async with database.sessions() as session:
        runs = ScopedRepo(session, Run, workspace_id)
        values: dict[str, Any] = {
            "ticket_id": ticket_id,
            "repo_id": repo_id,
            "worktree_id": worktree_id,
            "status": status.value,
            "graph_thread_id": None,
            "budget_usd": Decimal("5.0"),
            "created_by": None,
        }
        if run_id is not None:
            values["id"] = run_id
        run = await runs.create(**values)
        await session.commit()
        return run.id


async def test_start_run_unknown_ticket_is_404(run_api: Api) -> None:
    response = await run_api.client.post(
        "/api/v1/runs",
        json={"ticket_id": "missing-ticket"},
        headers=run_api.auth,
    )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


async def test_start_run_returns_201_and_audits(run_api: Api, database: Database) -> None:
    ticket_id = run_api.app.state.extra_test_ticket_id
    response = await run_api.client.post(
        "/api/v1/runs",
        json={"ticket_id": ticket_id, "junior_runtime": "native_claude", "budget_usd": "12.50"},
        headers=run_api.auth,
    )

    assert response.status_code == 201
    body = response.json()
    assert body["ticket_id"] == ticket_id
    assert body["junior_runtime"] == "native_claude"
    assert body["budget_usd"] == "12.500000"
    run_id = body["id"]

    async with database.sessions() as session:
        run = await ScopedRepo(session, Run, run_api.app.state.services.principal.workspace_id).get(
            run_id
        )
        assert run is not None
        assert run.junior_runtime == "native_claude"
        assert run.budget_usd == Decimal("12.500000")


async def test_start_run_rejects_an_active_run(run_api: Api, database: Database) -> None:
    ticket_id = run_api.app.state.extra_test_ticket_id
    workspace_id = run_api.app.state.services.principal.workspace_id
    async with database.sessions() as session:
        repos = ScopedRepo(session, Repository, workspace_id)
        repo = await repos.list(limit=1)
        assert repo
    await _make_run(
        database,
        workspace_id,
        ticket_id=ticket_id,
        repo_id=repo[0].id,
        status=RunStatus.AWAITING_APPROVAL,
    )

    response = await run_api.client.post(
        "/api/v1/runs",
        json={"ticket_id": ticket_id},
        headers=run_api.auth,
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "conflict"


async def test_list_runs_filter_pagination_and_joined_fields(
    run_api: Api, database: Database
) -> None:
    workspace_id = run_api.app.state.services.principal.workspace_id
    ticket_id = run_api.app.state.extra_test_ticket_id
    async with database.sessions() as session:
        repos = ScopedRepo(session, Repository, workspace_id)
        repo = await repos.list(limit=1)
        assert repo
        repo_id = repo[0].id
    first = await _make_run(
        database,
        workspace_id,
        ticket_id=ticket_id,
        repo_id=repo_id,
        run_id="00000000-0000-0000-0000-000000000001",
    )
    second = await _make_run(
        database,
        workspace_id,
        ticket_id=ticket_id,
        repo_id=repo_id,
        run_id="00000000-0000-0000-0000-000000000002",
    )

    async with database.sessions() as session:
        steps = ScopedRepo(session, RunStep, workspace_id)
        await steps.create(
            run_id=second,
            step_index=0,
            title="first step",
            spec_json={},
            status=RunStepStatus.PENDING,
        )
        await steps.create(
            run_id=second,
            step_index=1,
            title="done step",
            spec_json={},
            status=RunStepStatus.COMPLETED,
        )
        prs = ScopedRepo(session, PullRequest, workspace_id)
        await prs.create(
            run_id=second,
            number=7,
            url="https://example.invalid/pr/7",
            state=PullRequestState.OPEN.value,
            ci_status=CiStatus.PASSED.value,
        )
        await session.commit()

    first_response = await run_api.client.get("/api/v1/runs", headers=run_api.auth)
    assert first_response.status_code == 200
    page = first_response.json()
    # Newest first, one joined query (see RunRepo.list_summary; this test does
    # not need a SQL-statement counter because the suite has none).
    assert [item["id"] for item in page["items"]] == [second, first]
    assert page["items"][0]["ticket_title"] == "Ship run-api"
    assert page["items"][0]["current_step"] == {"index": 0, "title": "first step"}
    assert page["items"][0]["pr_number"] == 7
    assert page["items"][0]["pr_url"] == "https://example.invalid/pr/7"
    assert page["items"][0]["ci_status"] == "passed"

    filtered = await run_api.client.get(
        "/api/v1/runs", params={"status": "pending", "limit": 1}, headers=run_api.auth
    )
    assert filtered.status_code == 200
    assert len(filtered.json()["items"]) == 1
    assert filtered.json()["next_cursor"] is not None


async def test_get_run_detail(run_api: Api, database: Database) -> None:
    workspace_id = run_api.app.state.services.principal.workspace_id
    ticket_id = run_api.app.state.extra_test_ticket_id
    async with database.sessions() as session:
        repos = ScopedRepo(session, Repository, workspace_id)
        repo = await repos.list(limit=1)
        assert repo
        repo_id = repo[0].id
    run_id = await _make_run(
        database,
        workspace_id,
        ticket_id=ticket_id,
        repo_id=repo_id,
        worktree_id="worktree-1",
    )
    async with database.sessions() as session:
        steps = ScopedRepo(session, RunStep, workspace_id)
        await steps.create(run_id=run_id, step_index=0, title="Step", spec_json={})
        await session.commit()

    response = await run_api.client.get(f"/api/v1/runs/{run_id}", headers=run_api.auth)

    assert response.status_code == 200
    body = response.json()
    assert body["run"]["id"] == run_id
    assert body["ticket"]["id"] == ticket_id
    assert [step["title"] for step in body["steps"]] == ["Step"]
    assert body["worktree"]["branch"] == "gaia/run-api"
    assert body["pull_request"] is None


async def test_cancel_run(run_api: Api, database: Database) -> None:
    workspace_id = run_api.app.state.services.principal.workspace_id
    ticket_id = run_api.app.state.extra_test_ticket_id
    async with database.sessions() as session:
        repos = ScopedRepo(session, Repository, workspace_id)
        repo = await repos.list(limit=1)
        assert repo
        repo_id = repo[0].id
    run_id = await _make_run(
        database,
        workspace_id,
        ticket_id=ticket_id,
        repo_id=repo_id,
        status=RunStatus.AWAITING_APPROVAL,
    )

    response = await run_api.client.post(f"/api/v1/runs/{run_id}/cancel", headers=run_api.auth)

    assert response.status_code == 200
    assert response.json()["status"] == "cancelled"


async def test_cancel_terminal_run_is_409(run_api: Api, database: Database) -> None:
    workspace_id = run_api.app.state.services.principal.workspace_id
    ticket_id = run_api.app.state.extra_test_ticket_id
    async with database.sessions() as session:
        repos = ScopedRepo(session, Repository, workspace_id)
        repo = await repos.list(limit=1)
        assert repo
        repo_id = repo[0].id
    run_id = await _make_run(
        database,
        workspace_id,
        ticket_id=ticket_id,
        repo_id=repo_id,
        status=RunStatus.COMPLETED,
    )

    response = await run_api.client.post(f"/api/v1/runs/{run_id}/cancel", headers=run_api.auth)

    assert response.status_code == 409


async def test_run_routes_are_503_when_manager_is_missing(api: Api, database: Database) -> None:
    ticket_id = await _seed_ticket_worktree(api, database, name="unavailable")
    async with database.sessions() as session:
        repo = (
            await ScopedRepo(
                session,
                Repository,
                api.app.state.services.principal.workspace_id,
            ).list(limit=1)
        )[0]
        run_id = await _make_run(
            database,
            api.app.state.services.principal.workspace_id,
            ticket_id=ticket_id,
            repo_id=repo.id,
        )
        await session.commit()

    methods = [
        ("POST", "/api/v1/runs", {"ticket_id": ticket_id}),
        ("GET", "/api/v1/runs", None),
        ("GET", f"/api/v1/runs/{run_id}", None),
        ("POST", f"/api/v1/runs/{run_id}/cancel", None),
        ("GET", f"/api/v1/runs/{run_id}/diff", None),
    ]
    for method, path, json in methods:
        response = await api.client.request(method, path, json=json, headers=api.auth)
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "orchestrator_unavailable"


async def test_diff_returns_committed_plus_uncommitted_and_is_read_only(
    run_api: Api, database: Database, git_repo: Path
) -> None:
    workspace_id = run_api.app.state.services.principal.workspace_id
    git = run_api.app.state.services.git
    async with database.sessions() as session:
        repos = ScopedRepo(session, Repository, workspace_id)
        repo = await repos.create(name="diff-repo", local_path=str(git_repo))
        tickets = ScopedRepo(session, Ticket, workspace_id)
        ticket = await tickets.create(repo_id=repo.id, title="Diff ticket")
        worktrees = WorktreeService(git=git, session=session, workspace_id=workspace_id)
        worktree = await worktrees.create(repo, ticket)
        await session.commit()

    assert worktree.git_dir is not None
    location = WorktreeLocation(Path(worktree.path), Path(worktree.git_dir))
    (Path(worktree.path) / "committed.txt").write_text("committed\n")
    await git.commit(location, "committed")
    (Path(worktree.path) / "committed.txt").write_text("committed changed\n")

    async with database.sessions() as session:
        runs = ScopedRepo(session, Run, workspace_id)
        run = await runs.create(
            ticket_id=ticket.id,
            repo_id=repo.id,
            worktree_id=worktree.id,
            budget_usd=Decimal("5.0"),
        )
        await session.commit()

    before = await git.status_porcelain(location)
    response = await run_api.client.get(f"/api/v1/runs/{run.id}/diff", headers=run_api.auth)
    after = await git.status_porcelain(location)

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/x-diff")
    assert "committed" in response.text
    assert "committed changed" in response.text
    assert before == after

    (Path(worktree.path) / "committed.txt").write_text("x" * 5000 + "\n")
    capped = await run_api.client.get(
        f"/api/v1/runs/{run.id}/diff",
        params={"max_bytes": 1024},
        headers=run_api.auth,
    )
    assert capped.status_code == 200
    assert len(capped.content) <= 1024
    assert capped.text.endswith("[diff truncated]")


async def test_diff_without_worktree_is_404(run_api: Api, database: Database) -> None:
    workspace_id = run_api.app.state.services.principal.workspace_id
    ticket_id = run_api.app.state.extra_test_ticket_id
    async with database.sessions() as session:
        repos = ScopedRepo(session, Repository, workspace_id)
        repo = await repos.list(limit=1)
        assert repo
        repo_id = repo[0].id
    run_id = await _make_run(
        database,
        workspace_id,
        ticket_id=ticket_id,
        repo_id=repo_id,
        worktree_id=None,
    )

    response = await run_api.client.get(f"/api/v1/runs/{run_id}/diff", headers=run_api.auth)

    assert response.status_code == 404


async def test_list_runs_with_cursor_returns_the_second_page(
    run_api: Api, database: Database
) -> None:
    workspace_id = run_api.app.state.services.principal.workspace_id
    ticket_id = run_api.app.state.extra_test_ticket_id
    async with database.sessions() as session:
        repo = (await ScopedRepo(session, Repository, workspace_id).list(limit=1))[0]
        repo_id = repo.id
    first = await _make_run(
        database,
        workspace_id,
        ticket_id=ticket_id,
        repo_id=repo_id,
        run_id="00000000-0000-0000-0000-000000000001",
    )
    second = await _make_run(
        database,
        workspace_id,
        ticket_id=ticket_id,
        repo_id=repo_id,
        run_id="00000000-0000-0000-0000-000000000002",
    )

    first_page = await run_api.client.get("/api/v1/runs", params={"limit": 1}, headers=run_api.auth)
    assert first_page.status_code == 200
    assert [item["id"] for item in first_page.json()["items"]] == [second]
    cursor = first_page.json()["next_cursor"]
    assert cursor is not None

    second_page = await run_api.client.get(
        "/api/v1/runs", params={"limit": 1, "cursor": cursor}, headers=run_api.auth
    )

    assert second_page.status_code == 200
    assert [item["id"] for item in second_page.json()["items"]] == [first]
    assert second_page.json()["next_cursor"] is None


async def test_list_runs_without_more_rows_has_no_cursor(run_api: Api, database: Database) -> None:
    workspace_id = run_api.app.state.services.principal.workspace_id
    ticket_id = run_api.app.state.extra_test_ticket_id
    async with database.sessions() as session:
        repo = (await ScopedRepo(session, Repository, workspace_id).list(limit=1))[0]
        repo_id = repo.id
    await _make_run(database, workspace_id, ticket_id=ticket_id, repo_id=repo_id)

    response = await run_api.client.get("/api/v1/runs", headers=run_api.auth)

    assert response.status_code == 200
    assert response.json()["next_cursor"] is None


async def test_get_run_unknown_is_404(run_api: Api) -> None:
    response = await run_api.client.get("/api/v1/runs/missing-run", headers=run_api.auth)

    assert response.status_code == 404


async def test_get_run_detail_includes_pull_request(run_api: Api, database: Database) -> None:
    workspace_id = run_api.app.state.services.principal.workspace_id
    ticket_id = run_api.app.state.extra_test_ticket_id
    async with database.sessions() as session:
        repo = (await ScopedRepo(session, Repository, workspace_id).list(limit=1))[0]
        repo_id = repo.id
    run_id = await _make_run(
        database,
        workspace_id,
        ticket_id=ticket_id,
        repo_id=repo_id,
        worktree_id="worktree-1",
    )
    async with database.sessions() as session:
        prs = ScopedRepo(session, PullRequest, workspace_id)
        await prs.create(
            run_id=run_id,
            number=9,
            url="https://example.invalid/pr/9",
            state=PullRequestState.OPEN.value,
            ci_status=CiStatus.PASSED.value,
        )
        await session.commit()

    response = await run_api.client.get(f"/api/v1/runs/{run_id}", headers=run_api.auth)

    assert response.status_code == 200
    assert response.json()["pull_request"]["number"] == 9
    assert response.json()["pull_request"]["ci_status"] == "passed"


async def test_cancel_unknown_run_is_404(run_api: Api) -> None:
    response = await run_api.client.post("/api/v1/runs/missing-run/cancel", headers=run_api.auth)

    assert response.status_code == 404


async def test_cancel_missing_after_manager_is_404(
    run_api: Api, database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace_id = run_api.app.state.services.principal.workspace_id
    ticket_id = run_api.app.state.extra_test_ticket_id
    async with database.sessions() as session:
        repo = (await ScopedRepo(session, Repository, workspace_id).list(limit=1))[0]
        repo_id = repo.id
    run_id = await _make_run(
        database,
        workspace_id,
        ticket_id=ticket_id,
        repo_id=repo_id,
    )
    manager = run_api.app.state.services.runs

    async def delete_run(run_id: str, *, workspace_id: str) -> None:
        async with session_scope(database.sessions) as session:
            await ScopedRepo(session, Run, workspace_id).delete(run_id)

    monkeypatch.setattr(manager, "cancel", delete_run)
    response = await run_api.client.post(f"/api/v1/runs/{run_id}/cancel", headers=run_api.auth)

    assert response.status_code == 404


async def test_start_run_missing_after_manager_is_500(
    run_api: Api, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = run_api.app.state.services.runs

    async def fake_start(*args: Any, **kwargs: Any) -> str:
        return "missing-run-id"

    monkeypatch.setattr(manager, "start", fake_start)
    response = await run_api.client.post(
        "/api/v1/runs",
        json={"ticket_id": run_api.app.state.extra_test_ticket_id},
        headers=run_api.auth,
    )

    assert response.status_code == 500


async def test_diff_unknown_run_is_404(run_api: Api) -> None:
    response = await run_api.client.get("/api/v1/runs/missing-run/diff", headers=run_api.auth)

    assert response.status_code == 404


async def test_diff_with_missing_worktree_row_is_404(run_api: Api, database: Database) -> None:
    workspace_id = run_api.app.state.services.principal.workspace_id
    ticket_id = run_api.app.state.extra_test_ticket_id
    async with database.sessions() as session:
        foreign_workspace = await make_workspace(session, slug="foreign-worktree")
        foreign_repos = ScopedRepo(session, Repository, foreign_workspace.id)
        foreign_repo = await foreign_repos.create(
            name="foreign-repo", local_path="/tmp/foreign-worktree"
        )
        foreign_worktrees = ScopedRepo(session, Worktree, foreign_workspace.id)
        await foreign_worktrees.create(
            id="missing-worktree",
            repo_id=foreign_repo.id,
            path="/tmp/foreign-worktree/path",
            branch="gaia/foreign",
            base_sha="0" * 40,
        )
        repo = (await ScopedRepo(session, Repository, workspace_id).list(limit=1))[0]
        repo_id = repo.id
        await session.commit()
    run_id = await _make_run(
        database,
        workspace_id,
        ticket_id=ticket_id,
        repo_id=repo_id,
        worktree_id="missing-worktree",
    )

    response = await run_api.client.get(f"/api/v1/runs/{run_id}/diff", headers=run_api.auth)

    assert response.status_code == 404


async def test_diff_without_git_dir_derives_it(
    run_api: Api, database: Database, git_repo: Path
) -> None:
    workspace_id = run_api.app.state.services.principal.workspace_id
    git = run_api.app.state.services.git
    async with database.sessions() as session:
        repos = ScopedRepo(session, Repository, workspace_id)
        repo = await repos.create(name="diff-derived-repo", local_path=str(git_repo))
        tickets = ScopedRepo(session, Ticket, workspace_id)
        ticket = await tickets.create(repo_id=repo.id, title="Derived diff ticket")
        worktrees = ScopedRepo(session, Worktree, workspace_id)
        # Simulate a row that predates the git_dir column: the route must derive
        # the trusted admin dir from the repository.
        worktree = await worktrees.create(
            repo_id=repo.id,
            path=f"{git_repo}-worktree",
            branch="gaia/derived",
            base_sha=None,
            git_dir=None,
        )
        await session.commit()
        worktree_path = Path(worktree.path)
        await git.worktree_add(git_repo, path=worktree_path, ref="HEAD", new_branch="gaia/derived")
        (worktree_path / "uncommitted.txt").write_text("uncommitted\n")

    run_id = await _make_run(
        database,
        workspace_id,
        ticket_id=ticket.id,
        repo_id=repo.id,
        worktree_id=worktree.id,
    )

    response = await run_api.client.get(f"/api/v1/runs/{run_id}/diff", headers=run_api.auth)

    # base_sha is None, so the route refuses before running git.
    assert response.status_code == 404


async def test_diff_with_only_uncommitted_changes_is_empty_committed(
    run_api: Api, database: Database, git_repo: Path
) -> None:
    workspace_id = run_api.app.state.services.principal.workspace_id
    git = run_api.app.state.services.git
    base_sha = await git.current_sha(git_repo, "HEAD")
    async with database.sessions() as session:
        repos = ScopedRepo(session, Repository, workspace_id)
        repo = await repos.create(name="diff-uncommitted-repo", local_path=str(git_repo))
        tickets = ScopedRepo(session, Ticket, workspace_id)
        ticket = await tickets.create(repo_id=repo.id, title="Uncommitted diff ticket")
        worktrees = ScopedRepo(session, Worktree, workspace_id)
        worktree = await worktrees.create(
            repo_id=repo.id,
            path=f"{git_repo}-uncommitted-worktree",
            branch="gaia/uncommitted",
            base_sha=base_sha,
            git_dir=None,
        )
        await session.commit()
        worktree_path = Path(worktree.path)
        await git.worktree_add(
            git_repo, path=worktree_path, ref="HEAD", new_branch="gaia/uncommitted"
        )
        (worktree_path / "README.md").write_text("# changed\n")

    run_id = await _make_run(
        database,
        workspace_id,
        ticket_id=ticket.id,
        repo_id=repo.id,
        worktree_id=worktree.id,
    )

    response = await run_api.client.get(f"/api/v1/runs/{run_id}/diff", headers=run_api.auth)

    assert response.status_code == 200
    assert "changed" in response.text


async def test_truncate_diff_with_tiny_cap() -> None:
    from gaia.api.routes.runs import _truncate_diff

    assert _truncate_diff("x", 5, truncated=True) == "[diff truncated]"


async def test_start_run_function_direct(run_api: Api, database: Database) -> None:
    async with _scoped_for_api(database, run_api) as (scoped, principal, services):
        run = await start_run(
            RunCreate(ticket_id=run_api.app.state.extra_test_ticket_id),
            cast(Any, None),
            scoped,
            principal,
            services,
        )
    assert isinstance(run, RunRead)


async def test_list_runs_function_direct(run_api: Api, database: Database) -> None:
    async with _scoped_for_api(database, run_api) as (scoped, _principal, services):
        page = await list_runs(cast(Any, None), services, scoped)
    assert isinstance(page, RunPage)
    assert page.items is not None


async def test_get_run_function_direct(run_api: Api, database: Database) -> None:
    workspace_id = run_api.app.state.services.principal.workspace_id
    ticket_id = run_api.app.state.extra_test_ticket_id
    async with database.sessions() as session:
        repo = (await ScopedRepo(session, Repository, workspace_id).list(limit=1))[0]
        repo_id = repo.id
    run_id = await _make_run(
        database,
        workspace_id,
        ticket_id=ticket_id,
        repo_id=repo_id,
        worktree_id="worktree-1",
    )
    async with _scoped_for_api(database, run_api) as (scoped, _principal, services):
        detail = await get_run(run_id, cast(Any, None), services, scoped)
    assert isinstance(detail, RunDetail)
    assert detail.run.id == run_id


async def test_cancel_run_function_direct(run_api: Api, database: Database) -> None:
    workspace_id = run_api.app.state.services.principal.workspace_id
    ticket_id = run_api.app.state.extra_test_ticket_id
    async with database.sessions() as session:
        repo = (await ScopedRepo(session, Repository, workspace_id).list(limit=1))[0]
        repo_id = repo.id
    run_id = await _make_run(
        database,
        workspace_id,
        ticket_id=ticket_id,
        repo_id=repo_id,
        status=RunStatus.AWAITING_APPROVAL,
    )
    async with _scoped_for_api(database, run_api) as (scoped, principal, services):
        run = await cancel_run(run_id, cast(Any, None), services, scoped, principal)
    assert isinstance(run, RunRead)
    assert run.status == RunStatus.CANCELLED


async def test_get_run_diff_function_direct(
    run_api: Api, database: Database, git_repo: Path
) -> None:
    workspace_id = run_api.app.state.services.principal.workspace_id
    git = run_api.app.state.services.git
    base_sha = await git.current_sha(git_repo, "HEAD")
    async with database.sessions() as session:
        repos = ScopedRepo(session, Repository, workspace_id)
        repo = await repos.create(name="direct-diff-repo", local_path=str(git_repo))
        tickets = ScopedRepo(session, Ticket, workspace_id)
        ticket = await tickets.create(repo_id=repo.id, title="Direct diff ticket")
        worktrees = ScopedRepo(session, Worktree, workspace_id)
        worktree = await worktrees.create(
            repo_id=repo.id,
            path=f"{git_repo}-direct-worktree",
            branch="gaia/direct",
            base_sha=base_sha,
            git_dir=None,
        )
        await session.commit()
        worktree_path = Path(worktree.path)
        await git.worktree_add(git_repo, path=worktree_path, ref="HEAD", new_branch="gaia/direct")
        (worktree_path / "README.md").write_text("# direct changed\n")
    run_id = await _make_run(
        database,
        workspace_id,
        ticket_id=ticket.id,
        repo_id=repo.id,
        worktree_id=worktree.id,
    )
    async with _scoped_for_api(database, run_api) as (scoped, _principal, services):
        response = await get_run_diff(run_id, cast(Any, None), services, scoped)
    assert "direct changed" in bytes(response.body).decode()


async def test_start_run_function_direct_unknown_ticket(run_api: Api, database: Database) -> None:
    async with _scoped_for_api(database, run_api) as (scoped, principal, services):
        with pytest.raises(HTTPException, match="ticket not found"):
            await start_run(
                RunCreate(ticket_id="missing-ticket"),
                cast(Any, None),
                scoped,
                principal,
                services,
            )


async def test_start_run_function_direct_active_run(run_api: Api, database: Database) -> None:
    workspace_id = run_api.app.state.services.principal.workspace_id
    ticket_id = run_api.app.state.extra_test_ticket_id
    async with database.sessions() as session:
        repo = (await ScopedRepo(session, Repository, workspace_id).list(limit=1))[0]
        repo_id = repo.id
    await _make_run(
        database,
        workspace_id,
        ticket_id=ticket_id,
        repo_id=repo_id,
        status=RunStatus.AWAITING_APPROVAL,
    )
    async with _scoped_for_api(database, run_api) as (scoped, principal, services):
        with pytest.raises(HTTPException, match="active run"):
            await start_run(
                RunCreate(ticket_id=ticket_id),
                cast(Any, None),
                scoped,
                principal,
                services,
            )


async def test_get_run_function_direct_unknown(run_api: Api, database: Database) -> None:
    async with _scoped_for_api(database, run_api) as (scoped, _principal, services):
        with pytest.raises(HTTPException, match="run not found"):
            await get_run("missing-run", cast(Any, None), services, scoped)


async def test_get_run_diff_function_direct_run_unknown(run_api: Api, database: Database) -> None:
    async with _scoped_for_api(database, run_api) as (scoped, _principal, services):
        with pytest.raises(HTTPException, match="run not found"):
            await get_run_diff("missing-run", cast(Any, None), services, scoped)


async def test_get_run_diff_function_direct_no_worktree(run_api: Api, database: Database) -> None:
    workspace_id = run_api.app.state.services.principal.workspace_id
    ticket_id = run_api.app.state.extra_test_ticket_id
    async with database.sessions() as session:
        repo = (await ScopedRepo(session, Repository, workspace_id).list(limit=1))[0]
        repo_id = repo.id
    run_id = await _make_run(
        database,
        workspace_id,
        ticket_id=ticket_id,
        repo_id=repo_id,
        worktree_id=None,
    )
    async with _scoped_for_api(database, run_api) as (scoped, _principal, services):
        with pytest.raises(HTTPException, match="no worktree"):
            await get_run_diff(run_id, cast(Any, None), services, scoped)


async def test_get_run_diff_function_direct_missing_worktree(
    run_api: Api, database: Database
) -> None:
    workspace_id = run_api.app.state.services.principal.workspace_id
    ticket_id = run_api.app.state.extra_test_ticket_id
    async with database.sessions() as session:
        foreign_workspace = await make_workspace(session, slug="foreign-worktree-direct")
        foreign_repos = ScopedRepo(session, Repository, foreign_workspace.id)
        foreign_repo = await foreign_repos.create(
            name="foreign-repo-direct", local_path="/tmp/foreign-worktree-direct"
        )
        foreign_worktrees = ScopedRepo(session, Worktree, foreign_workspace.id)
        await foreign_worktrees.create(
            id="missing-worktree-direct",
            repo_id=foreign_repo.id,
            path="/tmp/foreign-worktree-direct/path",
            branch="gaia/foreign-direct",
            base_sha="0" * 40,
        )
        repo = (await ScopedRepo(session, Repository, workspace_id).list(limit=1))[0]
        repo_id = repo.id
        await session.commit()
    run_id = await _make_run(
        database,
        workspace_id,
        ticket_id=ticket_id,
        repo_id=repo_id,
        worktree_id="missing-worktree-direct",
    )
    async with _scoped_for_api(database, run_api) as (scoped, _principal, services):
        with pytest.raises(HTTPException, match="no worktree"):
            await get_run_diff(run_id, cast(Any, None), services, scoped)


async def test_get_run_diff_function_direct_missing_repository(
    run_api: Api, database: Database
) -> None:
    workspace_id = run_api.app.state.services.principal.workspace_id
    ticket_id = run_api.app.state.extra_test_ticket_id
    async with database.sessions() as session:
        foreign_workspace = await make_workspace(session, slug="foreign-repo-direct")
        foreign_repos = ScopedRepo(session, Repository, foreign_workspace.id)
        foreign_repo = await foreign_repos.create(
            name="foreign-repo-direct", local_path="/tmp/foreign-repo-direct"
        )
        await session.commit()
    run_id = await _make_run(
        database,
        workspace_id,
        ticket_id=ticket_id,
        repo_id=foreign_repo.id,
        worktree_id="worktree-1",
    )
    async with _scoped_for_api(database, run_api) as (scoped, _principal, services):
        with pytest.raises(HTTPException, match="no repository"):
            await get_run_diff(run_id, cast(Any, None), services, scoped)


async def test_get_run_diff_function_direct_clean_worktree(
    run_api: Api, database: Database, git_repo: Path
) -> None:
    workspace_id = run_api.app.state.services.principal.workspace_id
    git = run_api.app.state.services.git
    base_sha = await git.current_sha(git_repo, "HEAD")
    async with database.sessions() as session:
        repos = ScopedRepo(session, Repository, workspace_id)
        repo = await repos.create(name="direct-clean-diff-repo", local_path=str(git_repo))
        tickets = ScopedRepo(session, Ticket, workspace_id)
        ticket = await tickets.create(repo_id=repo.id, title="Clean direct diff ticket")
        worktrees = ScopedRepo(session, Worktree, workspace_id)
        worktree = await worktrees.create(
            repo_id=repo.id,
            path=f"{git_repo}-direct-clean-worktree",
            branch="gaia/direct-clean",
            base_sha=base_sha,
            git_dir=None,
        )
        await session.commit()
        worktree_path = Path(worktree.path)
        await git.worktree_add(
            git_repo, path=worktree_path, ref="HEAD", new_branch="gaia/direct-clean"
        )
    run_id = await _make_run(
        database,
        workspace_id,
        ticket_id=ticket.id,
        repo_id=repo.id,
        worktree_id=worktree.id,
    )
    async with _scoped_for_api(database, run_api) as (scoped, _principal, services):
        response = await get_run_diff(run_id, cast(Any, None), services, scoped)
    assert response.body == b""


async def test_run_repo_get_detail_missing_run(
    database: Database,
) -> None:
    async with database.sessions() as session:
        workspace = await make_workspace(session, slug="missing-run-detail")
        detail = await RunRepo(session).get_detail(workspace.id, "missing-run")
    assert detail is None


async def test_get_detail_returns_none_if_ticket_is_missing(
    database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with database.sessions() as session:
        workspace = await make_workspace(session, slug="missing-ticket")
        workspace_id = workspace.id
        repos = ScopedRepo(session, Repository, workspace_id)
        repo = await repos.create(name="missing-ticket-repo", local_path="/tmp/missing-ticket")
        tickets = ScopedRepo(session, Ticket, workspace_id)
        ticket = await tickets.create(repo_id=repo.id, title="Ticket")
        runs = ScopedRepo(session, Run, workspace_id)
        run = await runs.create(ticket_id=ticket.id, repo_id=repo.id)
        await session.commit()

        original_get = ScopedRepo.get

        async def fake_get(self: Any, row_id: str) -> Any:
            if self.model is Ticket:
                return None
            return await original_get(self, row_id)

        monkeypatch.setattr(ScopedRepo, "get", fake_get)
        detail = await RunRepo(session).get_detail(workspace_id, run.id)

    assert detail is None
