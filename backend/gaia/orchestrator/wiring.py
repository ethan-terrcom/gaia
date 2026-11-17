"""The one composition root that builds the running orchestrator (12b-3).

``build_wiring`` turns settings and the process-wide services into the real
:class:`~gaia.orchestrator.deps.GraphDeps` and a
:class:`~gaia.runtimes.base.RuntimeRegistry`. ADR-001 P2 lives here: which
runtime and model plays each role is read from ``Settings``, and this module
imports no vendor SDK. Vendor-bound adapters are imported only as Gaia classes.

The app lifespan calls :func:`build_wiring` exactly once and stores
:attr:`Wiring.approvals` on :class:`~gaia.api.deps.AppServices`, so doc 14's
decision route and an in-turn command approval always meet on the same
:class:`~gaia.runtimes.approvals.DbApprovalBroker` instance.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from gaia.config.secrets import SecretStore
from gaia.config.settings import Settings
from gaia.domain.enums import RuntimeKind
from gaia.orchestrator.auto_mode import AUTO_MODE_FILE, AutoModeSwitch, ModelFamily
from gaia.orchestrator.deps import GraphDeps, PullRequests
from gaia.orchestrator.impl.checks import SandboxCheckRunner
from gaia.orchestrator.impl.commits import WorktreeCommitter
from gaia.orchestrator.impl.planning import GatewayPlanner
from gaia.orchestrator.impl.review import GatewayReviewer
from gaia.orchestrator.impl.steps import RuntimeStepRunner
from gaia.orchestrator.impl.worktrees import WorktreeSetup
from gaia.orchestrator.pause import PauseSwitch
from gaia.runtimes.approvals import DbApprovalBroker
from gaia.runtimes.base import AgentTask, RuntimeRegistry
from gaia.runtimes.claude_agent_sdk import ClaudeAgentSdkRuntime
from gaia.runtimes.native import NativeRuntime
from gaia.runtimes.native_llm import gateway_llm_factory
from gaia.runtimes.orphans import OrphanRegistry
from gaia.security.audit import DbAuditSink
from gaia.security.commands import CommandPolicy
from gaia.security.sandbox import find_srt
from gaia.services.costs import PriceTable
from gaia.services.events import EventBus, EventSink
from gaia.services.git import GitService
from gaia.services.pull_requests import PullRequestService
from gaia.tools.fs import find_rg
from gaia.tools.registry import ALL_TOOLS, JUNIOR_TOOLS, ToolContext, build_default_registry
from gaia.tools.shell import CommandRunner, SrtCommandRunner

#: In-turn approval waits use the same generous default as the SDK runtime.


@dataclass(frozen=True)
class Wiring:
    """Everything the app needs from the composition root."""

    deps: GraphDeps
    runtimes: RuntimeRegistry
    approvals: DbApprovalBroker
    pause: PauseSwitch
    auto_mode: AutoModeSwitch


class LazySrtRunner:
    """A :class:`CommandRunner` that locates ``srt`` on first use.

    The app must start without ``srt`` installed (local-first, and the UI still
    works). A run that reaches a command then fails with the clear
    ``SandboxUnavailable`` "run ``make sandbox-install``" message instead of
    during startup.
    """

    def __init__(
        self,
        settings: Settings,
        *,
        allowed_domains: tuple[str, ...] = (),
    ) -> None:
        self._settings = settings
        self._allowed_domains = allowed_domains
        self._runner: SrtCommandRunner | None = None

    async def run(
        self,
        argv: Sequence[str],
        *,
        cwd: Path,
        timeout_s: float,
        task_dir: Path,
    ) -> Any:
        """Run ``argv`` through the lazily-built :class:`SrtCommandRunner`."""
        if self._runner is None:
            self._runner = SrtCommandRunner(
                self._settings,
                srt_path=find_srt(self._settings),
                allowed_domains=self._allowed_domains,
            )
        return await self._runner.run(
            argv,
            cwd=cwd,
            timeout_s=timeout_s,
            task_dir=task_dir,
        )


def _resolve_rg(settings: Settings) -> Path | None:
    """Locate ripgrep, turning a bad ``rg_path`` setting into a startup error."""
    try:
        return find_rg(settings)
    except ValueError as exc:
        raise ValueError(f"invalid rg_path setting: {exc}") from exc


def _native_task_dir(settings: Settings, task: AgentTask) -> Path:
    """Create the per-task scratch dir for the native runtime, mode 0700."""
    task_dir = settings.data_dir / "runs" / task.run_id / "tasks" / task.task_id
    task_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    task_dir.chmod(0o700)
    return task_dir


def build_wiring(
    *,
    settings: Settings,
    sessions: async_sessionmaker[AsyncSession],
    bus: EventBus,
    git: GitService,
    secrets: SecretStore,
    prices: PriceTable,
    # Test seams; production passes none of these.
    llm_http_client: Any | None = None,
    command_runner: CommandRunner | None = None,
    pull_requests: PullRequests | None = None,
) -> Wiring:
    """Build the production graph dependencies and runtime registry.

    Both runtimes are always registered under their :class:`RuntimeKind` value;
    ``RuntimeStepRunner`` then resolves the role's runtime from settings. The
    shared approval broker is created exactly once here and reused by both
    context factories, so an in-turn command approval and doc 14's decision
    route wait on the same in-process broker.
    """
    rg_path = _resolve_rg(settings)
    default_registry = build_default_registry(search_supports_regex=rg_path is not None)

    approvals = DbApprovalBroker(sessions, bus)
    pause = PauseSwitch(settings.data_dir / "paused")
    auto_mode = AutoModeSwitch(settings.data_dir / AUTO_MODE_FILE)
    audit = DbAuditSink(sessions)

    setup_runner = command_runner or LazySrtRunner(
        settings, allowed_domains=settings.setup_allowed_domains
    )
    check_runner = command_runner or LazySrtRunner(settings)
    tool_runner = command_runner or LazySrtRunner(settings)

    async def native_context_factory(task: AgentTask, events: EventSink) -> ToolContext:
        return ToolContext(
            task=task,
            git=git,
            policy=CommandPolicy.from_default(),
            approvals=approvals,
            audit=audit,
            events=events,
            commands=tool_runner,
            task_dir=_native_task_dir(settings, task),
            actor=f"agent:{task.role.value}",
            approval_timeout_s=settings.command_approval_timeout_s,
            rg_path=rg_path,
            # The native runtime is registered as ``native_deepseek`` (in demo
            # mode both roles use it), so its tasks follow the DeepSeek switch.
            auto_approve=partial(auto_mode.enabled, ModelFamily.DEEPSEEK),
        )

    async def claude_context_factory(
        task: AgentTask, events: EventSink, task_dir: Path
    ) -> ToolContext:
        return ToolContext(
            task=task,
            git=git,
            policy=CommandPolicy.from_default(),
            approvals=approvals,
            audit=audit,
            events=events,
            commands=tool_runner,
            task_dir=task_dir,
            actor=f"agent:{task.role.value}",
            approval_timeout_s=settings.command_approval_timeout_s,
            rg_path=rg_path,
            auto_approve=partial(auto_mode.enabled, ModelFamily.CLAUDE),
        )

    native = NativeRuntime(
        name=RuntimeKind.NATIVE_DEEPSEEK.value,
        llm_factory=gateway_llm_factory(
            settings=settings,
            sessions=sessions,
            price_table=prices,
            model=settings.junior_model,
            http_client=llm_http_client,
        ),
        tools=default_registry.subset(JUNIOR_TOOLS),
        context_factory=native_context_factory,
    )
    claude = ClaudeAgentSdkRuntime(
        name=RuntimeKind.CLAUDE_AGENT_SDK.value,
        registry=default_registry.subset(ALL_TOOLS),
        settings=settings,
        sessions=sessions,
        context_factory=claude_context_factory,
        model=settings.lead_model,
        price_table=prices,
        orphans=OrphanRegistry(settings.data_dir / "runtime-pids.json"),
        approval_timeout_s=settings.command_approval_timeout_s,
    )

    runtimes = RuntimeRegistry()
    runtimes.register(native)
    runtimes.register(claude)

    if pull_requests is None:
        pull_requests_impl: PullRequests = PullRequestService(
            sessions=sessions,
            git=git,
            secrets=secrets,
            bus=bus,
            settings=settings,
        )
    else:
        pull_requests_impl = pull_requests

    deps = GraphDeps(
        worktrees=WorktreeSetup(
            settings=settings,
            sessions=sessions,
            git=git,
            commands=setup_runner,
        ),
        planner=GatewayPlanner(
            settings=settings,
            sessions=sessions,
            price_table=prices,
            http_client=llm_http_client,
        ),
        steps=RuntimeStepRunner(
            settings=settings,
            sessions=sessions,
            git=git,
            runtimes=runtimes,
            bus=bus,
        ),
        checks=SandboxCheckRunner(
            settings=settings,
            sessions=sessions,
            commands=check_runner,
            policy=CommandPolicy.from_default(),
        ),
        reviewer=GatewayReviewer(
            settings=settings,
            sessions=sessions,
            price_table=prices,
            git=git,
            http_client=llm_http_client,
        ),
        committer=WorktreeCommitter(
            sessions=sessions,
            git=git,
        ),
        pull_requests=pull_requests_impl,
        sessions=sessions,
        events=bus,
        settings=settings,
    )
    return Wiring(
        deps=deps, runtimes=runtimes, approvals=approvals, pause=pause, auto_mode=auto_mode
    )
