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
from gaia.domain.enums import EventType, RunStatus
from gaia.domain.state import transition
from gaia.orchestrator.deps import GraphDeps
from gaia.orchestrator.state import RunState

logger = structlog.get_logger(__name__)


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

    def active(self) -> list[str]:
        """Return the ids of runs whose graph task is still running."""
        return [run_id for run_id, task in self._tasks.items() if not task.done()]

    async def start(self, ticket_id: str, *, workspace_id: str, created_by: str) -> str:
        """Create the run row and launch the graph in the background."""
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
                budget_usd=Decimal(str(self.deps.settings.run_budget_usd)),
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

        await self.deps.events.publish(
            workspace_id=workspace_id,
            type=EventType.RUN_STATUS_CHANGED,
            payload={"run_id": run_id, "status": RunStatus.CANCELLED.value},
            run_id=run_id,
        )

    async def recover(self) -> None:
        """Resume runs left active by a previous process, if they have no open interrupt."""
        async with session_scope(self.deps.sessions) as session:
            runs = await RunRepo(session).list_recoverable()

        active = set(self.active())
        for run in runs:
            if run.id in active:
                continue
            snapshot = await self.graph.aget_state(_config(run.id))
            has_interrupt = bool(snapshot.interrupts) or any(
                task.interrupts for task in snapshot.tasks
            )
            if has_interrupt:
                async with session_scope(self.deps.sessions) as session:
                    current = await RunRepo(session).get_by_id(run.id)
                    if current is not None:
                        transition(current, RunStatus.AWAITING_APPROVAL)
                continue
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
