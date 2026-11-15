"""``RunManager``: start, resume, cancel and recover graph runs (doc 12a)."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from decimal import Decimal
from typing import Any

import structlog
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Command

from gaia.config.settings import Settings
from gaia.db.engine import session_scope
from gaia.db.ids import uuid7
from gaia.db.models import Run, Ticket
from gaia.db.repo import RunRepo, ScopedRepo
from gaia.domain.enums import EventType, RunStatus, RuntimeKind
from gaia.domain.state import transition
from gaia.orchestrator.approvals import (
    expire_pending_approvals,
    expire_pending_command_approvals,
)
from gaia.orchestrator.deps import GraphDeps
from gaia.orchestrator.pause import OrchestratorPaused, PauseSwitch
from gaia.orchestrator.state import RunState

logger = structlog.get_logger(__name__)

#: Run statuses whose graph has ended; pause_all must not try to pause them if
#: a task happened to finish between ``active()`` and the database write.
TERMINAL_RUN_STATUSES = frozenset(
    {RunStatus.FAILED.value, RunStatus.COMPLETED.value, RunStatus.CANCELLED.value}
)


class RunBusy(RuntimeError):
    """The run already has a live graph task; resuming it again would race."""

    def __init__(self, run_id: str) -> None:
        self.run_id = run_id
        super().__init__(f"run {run_id!r} is already executing")


def _config(run_id: str) -> RunnableConfig:
    """Return the LangGraph config that uses ``run_id`` as the thread id."""
    return {"configurable": {"thread_id": run_id}}


def _initial_state(run_id: str, ticket_id: str, repo_id: str, workspace_id: str) -> RunState:
    """Return the initial graph state for a newly-created run."""
    return {
        "run_id": run_id,
        "ticket_id": ticket_id,
        "repo_id": repo_id,
        "workspace_id": workspace_id,
        "worktree_id": None,
        "plan": None,
        "step_index": 0,
        "attempt": 1,
        "feedback": None,
        "step_results": [],
        "review": None,
        "pr_number": None,
        "error": None,
        "route": None,
    }


@asynccontextmanager
async def open_checkpointer(settings: Settings) -> AsyncIterator[AsyncSqliteSaver]:
    """Open an :class:`AsyncSqliteSaver` on ``settings.checkpoint_path`` and set it up."""
    async with AsyncSqliteSaver.from_conn_string(str(settings.checkpoint_path)) as saver:
        await saver.setup()
        yield saver


class RunManager:
    """Owns the background tasks that drive the graph."""

    def __init__(
        self,
        graph: CompiledStateGraph[RunState, None, RunState, RunState],
        deps: GraphDeps,
        *,
        max_concurrent_runs: int | None = None,
        pause: PauseSwitch | None = None,
    ) -> None:
        self.graph = graph
        self.deps = deps
        self.max_concurrent_runs = (
            deps.settings.max_concurrent_runs
            if max_concurrent_runs is None
            else max_concurrent_runs
        )
        self._semaphore = asyncio.Semaphore(self.max_concurrent_runs)
        self._tasks: dict[str, asyncio.Task[Any]] = {}
        self._errors: dict[str, Exception] = {}
        self._pause = pause

    def active(self) -> list[str]:
        """Return the ids of runs whose graph task is still running."""
        return [run_id for run_id, task in self._tasks.items() if not task.done()]

    def _raise_if_paused(self) -> None:
        """Refuse to start or resume graph work while the kill switch is on."""
        if self._pause is not None and self._pause.is_paused():
            info = self._pause.info()
            raise OrchestratorPaused(info.reason if info else None)

    async def start(
        self,
        ticket_id: str,
        *,
        workspace_id: str,
        created_by: str,
        junior_runtime: RuntimeKind | None = None,
        budget_usd: Decimal | None = None,
    ) -> str:
        """Create the run row and launch the graph in the background."""
        self._raise_if_paused()
        run_id = uuid7()
        async with session_scope(self.deps.sessions) as session:
            tickets = ScopedRepo(session, Ticket, workspace_id)
            ticket = await tickets.get(ticket_id)
            if ticket is None:
                raise ValueError(f"no ticket {ticket_id!r} in workspace {workspace_id!r}")
            runs = ScopedRepo(session, Run, workspace_id)
            await runs.create(
                id=run_id,
                ticket_id=ticket_id,
                repo_id=ticket.repo_id,
                junior_runtime=junior_runtime.value if junior_runtime is not None else None,
                budget_usd=(
                    budget_usd
                    if budget_usd is not None
                    else Decimal(str(self.deps.settings.run_budget_usd))
                ),
                graph_thread_id=run_id,
                created_by=created_by,
            )

        self._launch(run_id, _initial_state(run_id, ticket_id, ticket.repo_id, workspace_id))
        return run_id

    async def resume(self, run_id: str, decision: dict[str, Any]) -> None:
        """Resume an interrupted run with ``decision``.

        Refuses a run whose graph task is still running: two ``ainvoke`` calls
        on one checkpoint thread would race (lead review 12a, R1). Doc 14 maps
        :class:`RunBusy` to 409.
        """
        self._raise_if_paused()
        if run_id in self.active():
            raise RunBusy(run_id)
        self._launch(run_id, Command(resume=decision))

    async def cancel(self, run_id: str, *, workspace_id: str) -> None:
        """Cancel the background task, if any, and mark the run cancelled.

        Scoped to ``workspace_id`` like every other write (F5, review 12a R3).
        """
        task = self._tasks.get(run_id)
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

        async with session_scope(self.deps.sessions) as session:
            run = await ScopedRepo(session, Run, workspace_id).get(run_id)
            if run is None:
                raise ValueError(f"no run {run_id!r}")
            transition(run, RunStatus.CANCELLED)
            expired = await expire_pending_approvals(
                session, workspace_id=workspace_id, run_id=run_id, reason="run cancelled"
            )

        await self.deps.events.publish(
            workspace_id=workspace_id,
            type=EventType.RUN_STATUS_CHANGED,
            payload={"run_id": run_id, "status": RunStatus.CANCELLED.value},
            run_id=run_id,
        )
        await _publish_expired(self.deps, workspace_id, run_id, expired)

    async def pause_all(self, *, reason: str) -> list[str]:
        """Pause every run that is currently doing graph work, plus queued runs.

        The flag is set first so nothing new can start while tasks are being
        cancelled. Live graph tasks are then cancelled and awaited, and in one
        database session those runs - plus every ``pending`` run without a task -
        are moved to ``paused``. Pending graph-gate approvals are left alone;
        pending in-turn command approvals of the paused runs are expired, because
        their agent process has just been cancelled. Runs already waiting at a
        graph gate or the budget gate are untouched, because the flag already
        blocks their decisions and their approval rows remain valid after a
        resume.
        """
        if self._pause is None:
            raise RuntimeError("pause switch is not configured")
        if not self._pause.is_paused():
            self._pause.pause(reason)

        live_ids = self.active()
        for run_id in live_ids:
            task = self._tasks[run_id]
            task.cancel()
        if live_ids:
            await asyncio.gather(
                *(self._tasks[run_id] for run_id in live_ids), return_exceptions=True
            )

        paused_ids: list[str] = []
        workspace_by_run: dict[str, str] = {}
        expired_by_run: dict[str, list[str]] = {}
        async with session_scope(self.deps.sessions) as session:
            repo = RunRepo(session)
            targets: list[Run] = []
            for run_id in live_ids:
                run = await repo.get_by_id(run_id)
                if run is not None and run.status not in TERMINAL_RUN_STATUSES:
                    targets.append(run)
            for run in await repo.list_recoverable():
                if run.status == RunStatus.PENDING.value and run.id not in live_ids:
                    targets.append(run)

            for run in targets:
                transition(run, RunStatus.PAUSED)
                expired = await expire_pending_command_approvals(
                    session,
                    workspace_id=run.workspace_id,
                    run_id=run.id,
                    reason="paused",
                )
                paused_ids.append(run.id)
                workspace_by_run[run.id] = run.workspace_id
                if expired:
                    expired_by_run[run.id] = expired

        for run_id in paused_ids:
            await self.deps.events.publish(
                workspace_id=workspace_by_run[run_id],
                type=EventType.RUN_STATUS_CHANGED,
                payload={"run_id": run_id, "status": RunStatus.PAUSED.value},
                run_id=run_id,
            )
        for run_id, approval_ids in expired_by_run.items():
            await _publish_expired(self.deps, workspace_by_run[run_id], run_id, approval_ids)

        return paused_ids

    async def resume_all(self) -> list[str]:
        """Clear the flag and relaunch every run it paused.

        Each run goes through the same checkpoint reconciliation as
        :meth:`recover`: an open interrupt becomes ``awaiting_approval``, and
        anything else is relaunched from its checkpoint (or from the initial
        state when it has none).
        """
        if self._pause is None:
            raise RuntimeError("pause switch is not configured")
        self._pause.resume()

        async with session_scope(self.deps.sessions) as session:
            runs = await RunRepo(session).list_by_status(RunStatus.PAUSED.value)

        for run in runs:
            await self._recover_run(run)
        return [run.id for run in runs]

    async def recover(self) -> None:
        """Resume runs left active by a previous process, unless the kill switch is on."""
        if self._pause is not None and self._pause.is_paused():
            info = self._pause.info()
            logger.info(
                "orchestrator.paused",
                reason=info.reason if info else None,
            )
            return

        async with session_scope(self.deps.sessions) as session:
            runs = await RunRepo(session).list_recoverable()

        for run in runs:
            await self._recover_run(run)

    async def _recover_run(self, run: Run) -> None:
        """Reconcile one run from its checkpoint and relaunch it when needed."""
        if run.id in self.active():
            return
        snapshot = await self.graph.aget_state(_config(run.id))
        has_interrupt = bool(snapshot.interrupts) or any(task.interrupts for task in snapshot.tasks)
        if has_interrupt:
            async with session_scope(self.deps.sessions) as session:
                current = await RunRepo(session).get_by_id(run.id)
                if current is not None:
                    transition(current, RunStatus.AWAITING_APPROVAL)
            return
        if snapshot.values:
            self._launch(run.id, None)
        else:
            self._launch(
                run.id,
                _initial_state(run.id, run.ticket_id, run.repo_id, run.workspace_id),
            )

    async def aclose(self) -> None:
        """Cancel and await every background task."""
        for task in self._tasks.values():
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks.values(), return_exceptions=True)
        self._tasks.clear()

    def _launch(self, run_id: str, input_: RunState | Command[Any] | None) -> None:
        """Start a graph invocation behind the concurrency semaphore."""

        async def _drive() -> None:
            async with self._semaphore:
                try:
                    await self.graph.ainvoke(input_, _config(run_id), durability="sync")
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    # RunFailed has already marked the run failed and emitted
                    # run.failed; anything else (a bad resume value, a
                    # checkpoint error) is logged so it never vanishes (R2).
                    self._errors[run_id] = exc
                    logger.error(
                        "orchestrator.run_errored",
                        run_id=run_id,
                        error_class=type(exc).__name__,
                        exc_info=exc,
                    )

        # Forget finished tasks so a long-lived manager does not grow (R4).
        for done_id in [key for key, task in self._tasks.items() if task.done()]:
            del self._tasks[done_id]
        self._tasks[run_id] = asyncio.create_task(_drive())


async def _publish_expired(
    deps: GraphDeps, workspace_id: str, run_id: str, approval_ids: list[str]
) -> None:
    """Tell the UI each expired approval left the inbox."""
    for approval_id in approval_ids:
        await deps.events.publish(
            workspace_id=workspace_id,
            type=EventType.APPROVAL_DECIDED,
            payload={"approval_id": approval_id, "action": "expired"},
            run_id=run_id,
        )
