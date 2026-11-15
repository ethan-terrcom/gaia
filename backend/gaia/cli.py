"""The ``gaia`` command line.

Owned by doc 02 (``secrets``) and doc 04 (``serve``, ``ui``). argparse from the
standard library, so the CLI adds no dependency.

Every command runs :func:`gaia.config.settings.ensure_data_dir` first, so
``~/.gaia`` is created owner-only before anything can write into it.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import sys
from collections.abc import Callable, Sequence
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from pydantic import ValidationError

import gaia
from gaia.config.secrets import (
    MissingSecretError,
    SecretName,
    SecretStore,
    ensure_api_token,
    get_secret_store,
)
from gaia.config.settings import Settings, ensure_data_dir, get_settings
from gaia.llm.base import LLMStop, StructuredLLM, UsageRecorder
from gaia.services.cleanup import CleanupPlan
from gaia.services.git import WorktreeLocation

PROG = "gaia"

#: What ``gaia serve`` hands uvicorn. A string rather than an app instance,
#: because ``--reload`` re-imports the target in a child process instead of
#: receiving a pickled app, and because the child must build its own settings.
APP_TARGET = "gaia.api.app:create_app"

_SECRET_NAMES = [name.value for name in SecretName]


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=PROG,
        description="Local-first control plane for a hierarchy of AI coding agents.",
    )
    parser.add_argument("--version", action="version", version=gaia.__version__)

    commands = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")

    secrets = commands.add_parser(
        "secrets",
        help="manage API keys in the OS keychain",
        description="Secrets live in the OS keychain (macOS Keychain), never in a file.",
    )
    secrets_commands = secrets.add_subparsers(
        dest="secrets_command", required=True, metavar="ACTION"
    )

    set_parser = secrets_commands.add_parser(
        "set",
        help="store a secret (the value is read from a prompt, never from argv)",
    )
    set_parser.add_argument("name", choices=_SECRET_NAMES, metavar="NAME")

    secrets_commands.add_parser(
        "list",
        help="show which secrets are set (never prints values)",
    )

    delete_parser = secrets_commands.add_parser("delete", help="remove a secret")
    delete_parser.add_argument("name", choices=_SECRET_NAMES, metavar="NAME")

    serve = commands.add_parser(
        "serve",
        help="run the API server on the host and port from Settings",
        description=(
            "Serves the API on 127.0.0.1 unless GAIA_ALLOW_REMOTE is set, because "
            "every requirement for a token on a LAN is a token in someone else's logs."
        ),
    )
    serve.add_argument(
        "--reload",
        action="store_true",
        help="restart when a Python file changes (development)",
    )

    commands.add_parser(
        "ui",
        help="print the one-time login URL for the UI",
        description=(
            "Prints <ui_origin>/#token=<token>. The token stays in the URL fragment, "
            "which a browser never sends to a server; the UI moves it into "
            "sessionStorage and strips it from the address bar (doc 16)."
        ),
    )

    github = commands.add_parser(
        "github",
        help="GitHub helpers for the local workspace (doc 15a)",
    )
    github_commands = github.add_subparsers(dest="github_command", required=True, metavar="ACTION")
    github_check = github_commands.add_parser(
        "check",
        help="check the GitHub token permissions for one repository",
        description=(
            "Loads a repository by name from the local workspace, reads GITHUB_TOKEN "
            "from the keychain, and prints a table of the fine-grained PAT "
            "permissions doc 15a requires. Exits 1 when any are missing."
        ),
    )
    github_check.add_argument(
        "--repo",
        required=True,
        help="repository name in the local workspace",
    )

    worktrees = commands.add_parser(
        "worktrees",
        help="inspect and clean up run worktrees and scratch directories (doc 18c)",
    )
    worktrees_commands = worktrees.add_subparsers(
        dest="worktrees_command", required=True, metavar="ACTION"
    )
    worktrees_cleanup = worktrees_commands.add_parser(
        "cleanup",
        help="plan or remove worktrees and scratch dirs of old finished runs",
        description=(
            "Prints a dry-run plan by default. Nothing is removed unless --apply "
            "is given. Never removes dirty, tampered or unmerged-work worktrees."
        ),
    )
    worktrees_cleanup.add_argument(
        "--older-than",
        type=int,
        default=None,
        metavar="DAYS",
        help="only consider runs finished at least this many days ago "
        "(default: cleanup_older_than_days from Settings)",
    )
    worktrees_cleanup.add_argument(
        "--apply",
        action="store_true",
        help="remove the planned worktrees and scratch directories",
    )

    demo = commands.add_parser(
        "demo",
        help="first-run demo helpers (18b-2)",
    )
    demo_commands = demo.add_subparsers(dest="demo_command", required=True, metavar="ACTION")
    demo_commands.add_parser(
        "init",
        help="create the local demo repository, registration and ticket",
        description=(
            "Creates {data_dir}/demo/greet, a tiny git repo with one commit, "
            "registers it as a Gaia repository and opens one ticket. Running it "
            "again reuses the same rows. It works with or without "
            "GAIA_FAKE_LLM=1."
        ),
    )

    dev = commands.add_parser(
        "dev",
        help="development helpers that are only available when the server enables them",
    )
    dev_commands = dev.add_subparsers(dest="dev_command", required=True, metavar="ACTION")
    fake_run = dev_commands.add_parser(
        "fake-run",
        help="ask a running Gaia server to publish a scripted run",
        description=(
            "The server must be started with GAIA_DEV_ROUTES=true. This command "
            "reads the API token from the keychain and posts with it; it never "
            "prints the token."
        ),
    )
    fake_run.add_argument("--events", type=int, default=3, help="number of agent.message events")
    fake_run.add_argument("--interval", type=float, default=0.2, help="seconds between events")

    plan = dev_commands.add_parser(
        "plan",
        help="plan a ticket with the lead model and print the JSON plan",
        description=(
            "Loads the ticket and repository from GAIA_DATA_DIR, builds a read-only "
            "repository context, and calls the lead model once with structured outputs."
        ),
    )
    plan.add_argument("ticket_id", help="ticket id in the local workspace")
    run_task = dev_commands.add_parser(
        "run-task",
        help="run one agent task in a worktree",
        description=(
            "The fake runtime is local and needs no server. The native runtime "
            "calls the model through the LLM gateway, so `gaia serve` must "
            "already be running on Settings.host:Settings.port."
        ),
    )
    run_task.add_argument(
        "--runtime", choices=["fake", "native", "claude_sdk"], default="fake", help="runtime to use"
    )
    run_task.add_argument("--model", default="deepseek-v4-pro", help="model for native runtime")
    run_task.add_argument(
        "--gateway-base",
        default=None,
        help="Gaia server origin serving the gateway, e.g. http://127.0.0.1:8799 (native)",
    )
    run_task.add_argument("--worktree", required=True, help="a Gaia worktree path to run in")
    run_task.add_argument("instructions", help="the task instructions")

    return parser


def _secrets_set(args: argparse.Namespace, store: SecretStore) -> int:
    name = SecretName(args.name)
    if name is SecretName.GAIA_API_TOKEN:
        # Gaia generates this token itself so the UI never has to be told it.
        print(
            f"{name.value} is generated by Gaia and cannot be set by hand.",
            file=sys.stderr,
        )
        return 1
    # getpass reads from the terminal without echoing; the value never appears
    # in argv, in a shell history or in the environment.
    try:
        value = getpass.getpass(f"{name.value}: ")
    except EOFError:
        # Ctrl-D, or no terminal at all (a pipe, an editor task runner): say so
        # rather than escaping with a traceback.
        print(f"error: no value entered for {name.value}.", file=sys.stderr)
        return 1
    try:
        store.set(name, value)
    except ValueError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    print(f"Stored {name.value} in the keychain.")
    return 0


def _secrets_list(args: argparse.Namespace, store: SecretStore) -> int:
    for name in SecretName:
        status = "set" if store.get(name) is not None else "not set"
        print(f"{name.value:<22} {status}")
    return 0


def _secrets_delete(args: argparse.Namespace, store: SecretStore) -> int:
    name = SecretName(args.name)
    if not store.delete(name):
        print(f"error: {name.value} is not set.", file=sys.stderr)
        return 1
    print(f"Deleted {name.value} from the keychain.")
    return 0


_SECRETS_HANDLERS = {
    "set": _secrets_set,
    "list": _secrets_list,
    "delete": _secrets_delete,
}


def _secrets(args: argparse.Namespace) -> int:
    store = get_secret_store()
    handler = _SECRETS_HANDLERS[args.secrets_command]
    return handler(args, store)


async def _github_check_async(args: argparse.Namespace) -> int:
    """Async body of ``gaia github check``.

    stdout carries only the permission table, so it can be piped; logs go to
    stderr. The token is read from the keychain, never printed, and only used as
    the bearer credential for the API calls.
    """
    from gaia.config.logging import configure_logging
    from gaia.db.engine import create_engine_for, create_session_factory, session_scope
    from gaia.db.models import Repository
    from gaia.db.repo import ScopedRepo
    from gaia.db.seed import seed_initial_data
    from gaia.services.github import (
        GitHubClient,
        UnsupportedRemote,
        check_token,
        parse_github_remote,
    )

    configure_logging()
    settings = get_settings()
    store = get_secret_store()
    engine = create_engine_for(settings)
    sessions = create_session_factory(engine)
    try:
        async with session_scope(sessions) as session:
            seeded = await seed_initial_data(session, settings)
            repositories = ScopedRepo(session, Repository, seeded.workspace.id)
            rows = await repositories.list(name=args.repo)
            if not rows:
                print(
                    f"error: no repository named {args.repo!r} in workspace "
                    f"{seeded.workspace.slug!r}",
                    file=sys.stderr,
                )
                return 1
            repository = rows[0]
            if not repository.remote_url:
                print("error: this repository has no GitHub remote", file=sys.stderr)
                return 1
            try:
                slug = parse_github_remote(repository.remote_url)
            except UnsupportedRemote:
                print("error: this repository has no GitHub remote", file=sys.stderr)
                return 1

            token = store.require(SecretName.GITHUB_TOKEN)
            async with GitHubClient(token) as client:
                result = await check_token(client, slug)

            permissions = (
                "Metadata R",
                "Contents R/W",
                "Pull requests R",
                "Issues R",
                "Commit statuses R",
                "Checks R",
            )
            for permission in permissions:
                status = "ok" if permission not in result.missing else "missing"
                print(f"{permission:<20} {status}")
            if not result.ok:
                print(
                    "GitHub token is missing permissions. See "
                    "docs/implementation/15-github-integration.md for the required "
                    "fine-grained PAT scopes.",
                    file=sys.stderr,
                )
                return 1
            return 0
    except MissingSecretError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    finally:
        await engine.dispose()


def _github_check(args: argparse.Namespace) -> int:
    """Run the GitHub permission check against the real keychain and GitHub."""
    return asyncio.run(_github_check_async(args))


def _github(args: argparse.Namespace) -> int:
    if args.github_command == "check":
        return _github_check(args)
    return 2


async def _worktrees_cleanup_async(args: argparse.Namespace) -> int:
    """Async body of ``gaia worktrees cleanup``.

    stdout carries the plan (and, with ``--apply``, the result); logs go to
    stderr. The command opens the database like the other commands and builds the
    real :class:`~gaia.services.git.GitService`, because the plan must look at
    the actual worktrees rather than only their rows.
    """
    from gaia.config.logging import configure_logging
    from gaia.db.engine import create_engine_for, create_session_factory, session_scope
    from gaia.db.seed import seed_initial_data
    from gaia.services.cleanup import CleanupService
    from gaia.services.git import GitService

    configure_logging()
    settings = get_settings()
    older_than = settings.cleanup_older_than_days if args.older_than is None else args.older_than
    if older_than < 1:
        print("error: --older-than must be at least 1", file=sys.stderr)
        return 2

    engine = create_engine_for(settings)
    sessions = create_session_factory(engine)
    try:
        async with session_scope(sessions) as session:
            await seed_initial_data(session, settings)

        service = CleanupService(
            sessions=sessions,
            git=GitService(settings),
            settings=settings,
        )
        plan = await service.plan(older_than_days=older_than)
        _print_cleanup_plan(plan)

        if not args.apply:
            print("Dry run: nothing was removed. Pass --apply to remove the plan.")
            return 0

        result = await service.apply(plan)
        print(
            f"Removed {result.worktrees_removed} worktree(s) and "
            f"{result.scratch_dirs_removed} scratch dir(s); "
            f"{len(result.skips)} item(s) skipped."
        )
        for skip in result.skips:
            print(f"  skipped {skip.path}: {skip.reason}")
        return 0
    finally:
        await engine.dispose()


def _print_cleanup_plan(plan: CleanupPlan) -> None:
    """Print a ``CleanupPlan`` as a small fixed-width table."""
    print("Worktrees")
    print(f"{'PATH':<60} {'BRANCH':<40} {'LAST STATUS':<14} {'FINISHED':<20} SKIP")
    for item in plan.worktrees:
        print(
            f"{item.path!s:<60} {item.worktree.branch:<40} "
            f"{item.last_run.status:<14} {_fmt_dt(item.last_run.finished_at):<20}"
        )
    for skip in plan.skipped:
        if skip.worktree is None:
            continue
        branch = skip.worktree.branch
        status = skip.last_run.status if skip.last_run is not None else ""
        finished = _fmt_dt(skip.last_run.finished_at) if skip.last_run is not None else ""
        print(f"{skip.path!s:<60} {branch:<40} {status:<14} {finished:<20} {skip.reason}")
    if plan.scratch_dirs:
        print("Scratch directories")
        for path in plan.scratch_dirs:
            print(f"  {path}")
    else:
        print("Scratch directories: none")


def _fmt_dt(value: datetime | None) -> str:
    """Render an optional datetime for the plan table."""
    if value is None:
        return ""
    return value.isoformat(timespec="seconds")


def _worktrees_cleanup(args: argparse.Namespace) -> int:
    """Run the cleanup async body."""
    return asyncio.run(_worktrees_cleanup_async(args))


def _worktrees(args: argparse.Namespace) -> int:
    if args.worktrees_command == "cleanup":
        return _worktrees_cleanup(args)
    return 2


def _demo_init(args: argparse.Namespace) -> int:
    """Run ``gaia demo init`` against the configured data directory."""
    del args
    from gaia.config.logging import configure_logging
    from gaia.dev.demo import init_demo

    # Like every other command: INFO and up, redacted. Without this, structlog's
    # default prints debug lines (proc.finished ...) to a first-time user.
    configure_logging()
    return asyncio.run(init_demo(get_settings()))


def _demo(args: argparse.Namespace) -> int:
    if args.demo_command == "init":
        return _demo_init(args)
    return 2


def _ui(args: argparse.Namespace) -> int:
    """Print the one-time login URL for the UI (doc 04 task 2).

    The token is generated here if the server has not run yet, so the order of
    ``make dev-frontend``, ``make dev-backend`` and ``gaia ui`` does not matter.

    Printed, not opened. Opening it would mean handing the token to a browser
    process's argv, where ``ps`` can read it - the same token doc 04 keeps out of
    URLs and SSE. Doc 16 owns the login flow that decides how the UI gets it.
    """
    settings = get_settings()
    token = ensure_api_token(get_secret_store())
    print(f"{settings.ui_origin.rstrip('/')}/#token={token.get_secret_value()}")
    # TODO(gaia-16): the login flow is now the #token= fragment stripped by
    # bootstrapToken. --open stays out because a token must not appear in a
    # process's argv; the owner copies the printed URL instead.
    return 0


def _serve(args: argparse.Namespace) -> int:
    """Run the API with uvicorn, on the host and port Settings allows.

    ``Settings`` rejects anything but a loopback address unless ``allow_remote``
    is set (doc 02), so a bad ``GAIA_HOST`` stops the server here rather than
    binding it to the LAN.
    """
    # Imported here so `gaia secrets` and `gaia ui` do not pay for the server
    # stack (uvicorn pulls in a lot of it).
    import uvicorn

    from gaia.config.logging import configure_logging

    settings = get_settings()
    configure_logging(
        log_dir=settings.log_dir,
        max_bytes=settings.log_file_max_bytes,
        backups=settings.log_file_backups,
    )
    uvicorn.run(
        APP_TARGET,
        factory=True,
        host=settings.host,
        port=settings.port,
        reload=args.reload,
        log_config=None,
    )
    return 0


def _dev_fake_run(args: argparse.Namespace) -> int:
    """Ask a running Gaia server to publish a scripted fake run (doc 13)."""
    # Imported here so the other CLI commands do not pay for httpx.
    import httpx

    settings = get_settings()
    token = ensure_api_token(get_secret_store())
    url = f"http://{settings.host}:{settings.port}/api/v1/dev/fake-run"
    try:
        response = httpx.post(
            url,
            json={"events": args.events, "interval_s": args.interval},
            headers={"Authorization": f"Bearer {token.get_secret_value()}"},
        )
    except httpx.HTTPError as error:
        print(f"error: could not reach Gaia at {url}: {error}", file=sys.stderr)
        return 1
    if response.status_code != 200:
        print(f"error: Gaia answered {response.status_code}", file=sys.stderr)
        return 1
    try:
        body = response.json()
    except ValueError:
        print("error: Gaia answered with a non-JSON body.", file=sys.stderr)
        return 1
    print(body["run_id"])
    return 0


async def _make_client(
    settings: object,
    store: SecretStore,
    recorder: UsageRecorder,
) -> StructuredLLM:
    """Build the real Anthropic client for ``gaia dev plan``.

    Imported lazily so the other CLI commands do not pay for the Anthropic SDK.
    ``settings`` is typed as ``object`` so tests can monkeypatch it with a fake
    settings object; the real function only reads ``lead_model``.
    """
    from gaia.llm.anthropic_client import AnthropicClient
    from gaia.services.costs import load_price_table

    settings_model = getattr(settings, "lead_model", None)
    if settings_model is None:
        raise ValueError("settings.lead_model is required for gaia dev plan")
    return AnthropicClient(
        api_key=store.require(SecretName.ANTHROPIC_API_KEY),
        model=str(settings_model),
        recorder=recorder,
        price_table=load_price_table(),
    )


def _dev_plan(args: argparse.Namespace) -> int:
    """Plan a ticket with the lead model, then leave the run cancelled."""
    return asyncio.run(_dev_plan_async(args))


async def _dev_plan_async(args: argparse.Namespace) -> int:
    """Async body of ``gaia dev plan``."""
    from gaia.config.logging import configure_logging
    from gaia.db.engine import create_engine_for, create_session_factory, session_scope
    from gaia.db.models import Agent, Repository, Run, Ticket
    from gaia.db.models import LlmUsage as LlmUsageRow
    from gaia.db.repo import ScopedRepo
    from gaia.db.seed import seed_initial_data
    from gaia.domain.enums import RunStatus
    from gaia.domain.plans import TicketInput
    from gaia.domain.state import transition
    from gaia.orchestrator.lead import LeadEngineer, LeadEscalation, PlanRejected
    from gaia.orchestrator.repo_context import build_repo_context
    from gaia.security.commands import CommandPolicy
    from gaia.services.costs import DbUsageRecorder

    # stdout carries the plan JSON (pipe it to jq); logs go to stderr.
    configure_logging()
    settings = get_settings()
    store = get_secret_store()
    engine = create_engine_for(settings)
    sessions = create_session_factory(engine)
    try:
        async with session_scope(sessions) as session:
            seeded = await seed_initial_data(session, settings)
            workspace_id = seeded.workspace.id

            tickets = ScopedRepo(session, Ticket, workspace_id)
            ticket_row = await tickets.get(args.ticket_id)
            if ticket_row is None:
                print(
                    f"error: no ticket {args.ticket_id!r} in workspace {seeded.workspace.slug!r}",
                    file=sys.stderr,
                )
                return 1

            repositories = ScopedRepo(session, Repository, workspace_id)
            repository = await repositories.get(ticket_row.repo_id)
            if repository is None:
                print(
                    f"error: ticket {ticket_row.id!r} has no repository {ticket_row.repo_id!r}",
                    file=sys.stderr,
                )
                return 1

            agents = ScopedRepo(session, Agent, workspace_id)
            lead_agents = await agents.list(display_name="lead")
            if not lead_agents:
                print("error: the seeded lead agent is missing", file=sys.stderr)
                return 1
            lead_agent = lead_agents[0]

            runs = ScopedRepo(session, Run, workspace_id)
            run = await runs.create(
                ticket_id=ticket_row.id,
                repo_id=repository.id,
                budget_usd=Decimal(str(settings.run_budget_usd)),
            )
            transition(run, RunStatus.PLANNING)

            ticket = TicketInput(id=ticket_row.id, title=ticket_row.title, body=ticket_row.body)
            repo_context = await build_repo_context(Path(repository.local_path), ticket)
            recorder = DbUsageRecorder(session, workspace_id, run.id, agent_id=lead_agent.id)
            client = await _make_client(settings, store, recorder)
            policy = CommandPolicy.from_default()
            extra_allow: Sequence[Sequence[str]] = ()
            if repository.test_command:
                extra_allow = (repository.test_command,)

            plan = await LeadEngineer(client, policy).plan_ticket(
                ticket,
                repo_context,
                extra_allow=extra_allow,
            )

            usage_rows = await ScopedRepo(session, LlmUsageRow, workspace_id).list(run_id=run.id)
            # Every prompt token: uncached, written to the cache, and read from it.
            input_tokens = sum(
                row.input_tokens + row.cache_creation_tokens + row.cache_read_tokens
                for row in usage_rows
            )
            output_tokens = sum(row.output_tokens for row in usage_rows)
            cost_usd = sum((row.cost_usd for row in usage_rows), Decimal(0))
            print(json.dumps(plan.model_dump(mode="json"), indent=2))
            print(f"cost: ${cost_usd:.4f} ({input_tokens} prompt / {output_tokens} output tokens)")
            transition(run, RunStatus.CANCELLED)
            return 0
    except (LeadEscalation, PlanRejected, MissingSecretError, LLMStop) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    finally:
        await engine.dispose()


def _worktree_location(path: Path) -> WorktreeLocation:
    """Build a :class:`WorktreeLocation` from a Gaia worktree path.

    The CLI is a developer helper, so it accepts only a real Gaia worktree: the
    ``.git`` file must point at the trusted admin directory, exactly as
    :class:`GitService` requires before it will run inside a worktree.
    """
    work_tree = path.expanduser().resolve()
    git_file = work_tree / ".git"
    if not git_file.is_file():
        raise ValueError(f"{str(work_tree)!r} is not a Gaia worktree (no .git file)")
    content = git_file.read_text()
    if not content.startswith("gitdir: "):
        raise ValueError(f"{str(work_tree)!r} is not a Gaia worktree (malformed .git file)")
    git_dir = Path(content.removeprefix("gitdir: ").strip()).expanduser().resolve()
    return WorktreeLocation(work_tree=work_tree, git_dir=git_dir)


async def _run_fake_task(args: argparse.Namespace, settings: Settings) -> int:
    """Create a scratch run and execute the fake runtime in a worktree."""
    from gaia.api.app import create_app
    from gaia.db.engine import session_scope
    from gaia.db.ids import uuid7
    from gaia.db.models import Repository, Run, Ticket
    from gaia.db.repo import ScopedRepo
    from gaia.domain.enums import AgentRole, EventType
    from gaia.runtimes.approvals import InMemoryApprovalBroker
    from gaia.runtimes.base import AgentTask
    from gaia.runtimes.fake import Emit, FakeAction, FakeRuntime, Finish, WriteFile
    from gaia.runtimes.support import run_with_limits
    from gaia.services.events import BusEventSink

    app = create_app(settings)
    async with app.router.lifespan_context(app):
        services = app.state.services
        location = _worktree_location(Path(args.worktree))
        base_sha = await services.git.current_sha(location)
        async with session_scope(services.sessions) as session:
            repos = ScopedRepo(session, Repository, services.principal.workspace_id)
            repository = await repos.create(
                name=f"dev-run-task-{uuid7()}",
                local_path=str(location.work_tree),
                created_by=services.principal.user_id,
            )
            tickets = ScopedRepo(session, Ticket, services.principal.workspace_id)
            ticket = await tickets.create(
                repo_id=repository.id,
                title="Dev run-task",
                created_by=services.principal.user_id,
            )
            runs = ScopedRepo(session, Run, services.principal.workspace_id)
            run = await runs.create(
                ticket_id=ticket.id,
                repo_id=repository.id,
                created_by=services.principal.user_id,
            )

        task = AgentTask(
            task_id=uuid7(),
            run_id=run.id,
            run_step_id=None,
            workspace_id=services.principal.workspace_id,
            agent_id=None,
            role=AgentRole.JUNIOR,
            instructions=args.instructions,
            worktree=location,
            base_sha=base_sha,
        )
        script: list[FakeAction] = [
            Emit(EventType.AGENT_MESSAGE, {"content": "writing hello.py"}),
            WriteFile("hello.py", "print('hello')\n"),
            Finish("wrote hello.py", None),
        ]
        runtime = FakeRuntime(script, InMemoryApprovalBroker())
        sink = BusEventSink(
            services.events,
            workspace_id=services.principal.workspace_id,
            run_id=run.id,
        )
        result = await run_with_limits(runtime, task, sink, git=services.git)
        print(result.model_dump_json(indent=2))
        return 0


async def _run_native_task(args: argparse.Namespace, settings: Settings) -> int:
    """Create a scratch run and execute the native runtime in a worktree."""
    import shutil
    import tempfile

    from gaia.api.app import create_app
    from gaia.db.engine import session_scope
    from gaia.db.ids import uuid7
    from gaia.db.models import Repository, Run, Ticket
    from gaia.db.repo import ScopedRepo
    from gaia.domain.enums import AgentRole
    from gaia.runtimes.approvals import DbApprovalBroker
    from gaia.runtimes.base import AgentTask
    from gaia.runtimes.native import NativeRuntime
    from gaia.runtimes.native_llm import gateway_llm_factory
    from gaia.runtimes.support import run_with_limits
    from gaia.security.audit import DbAuditSink
    from gaia.security.commands import CommandPolicy
    from gaia.security.sandbox import find_srt
    from gaia.services.events import BusEventSink, EventSink
    from gaia.tools.fs import find_rg
    from gaia.tools.registry import JUNIOR_TOOLS, ToolContext, build_default_registry
    from gaia.tools.shell import SrtCommandRunner

    app = create_app(settings)
    async with app.router.lifespan_context(app):
        services = app.state.services
        location = _worktree_location(Path(args.worktree))
        base_sha = await services.git.current_sha(location)
        async with session_scope(services.sessions) as session:
            repos = ScopedRepo(session, Repository, services.principal.workspace_id)
            repository = await repos.create(
                name=f"dev-run-task-{uuid7()}",
                local_path=str(location.work_tree),
                created_by=services.principal.user_id,
            )
            tickets = ScopedRepo(session, Ticket, services.principal.workspace_id)
            ticket = await tickets.create(
                repo_id=repository.id,
                title="Dev run-task",
                created_by=services.principal.user_id,
            )
            runs = ScopedRepo(session, Run, services.principal.workspace_id)
            run = await runs.create(
                ticket_id=ticket.id,
                repo_id=repository.id,
                created_by=services.principal.user_id,
                budget_usd=Decimal(str(settings.run_budget_usd)),
            )

        task = AgentTask(
            task_id=uuid7(),
            run_id=run.id,
            run_step_id=None,
            workspace_id=services.principal.workspace_id,
            agent_id=None,
            role=AgentRole.JUNIOR,
            instructions=args.instructions,
            worktree=location,
            base_sha=base_sha,
        )
        task_dir = Path(tempfile.mkdtemp(prefix="gaia-native-task-"))
        try:
            srt_path = find_srt(settings)
            registry = build_default_registry(
                search_supports_regex=find_rg(settings) is not None
            ).subset(JUNIOR_TOOLS)
            broker = DbApprovalBroker(services.sessions, services.events)
            audit = DbAuditSink(services.sessions)

            async def context_factory(task: AgentTask, events: EventSink) -> ToolContext:
                return ToolContext(
                    task=task,
                    git=services.git,
                    policy=CommandPolicy.from_default(),
                    approvals=broker,
                    audit=audit,
                    events=events,
                    commands=SrtCommandRunner(settings, srt_path=srt_path),
                    task_dir=task_dir,
                    actor=f"agent:{task.agent_id or 'junior'}",
                    rg_path=find_rg(settings),
                )

            llm_factory = gateway_llm_factory(
                settings=settings,
                sessions=services.sessions,
                price_table=services.prices,
                model=args.model,
                gateway_base=args.gateway_base,
            )
            runtime = NativeRuntime(
                tools=registry,
                llm_factory=llm_factory,
                context_factory=context_factory,
            )
            sink = BusEventSink(
                services.events,
                workspace_id=services.principal.workspace_id,
                run_id=run.id,
            )
            result = await run_with_limits(runtime, task, sink, git=services.git)
            print(result.model_dump_json(indent=2))
            return 0
        finally:
            shutil.rmtree(task_dir, ignore_errors=True)


async def _run_claude_sdk_task(args: argparse.Namespace, settings: Settings) -> int:
    """Run one lead task through the Claude Agent SDK runtime."""
    from gaia.api.app import create_app
    from gaia.db.engine import session_scope
    from gaia.db.ids import uuid7
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

    app = create_app(settings)
    async with app.router.lifespan_context(app):
        services = app.state.services
        location = _worktree_location(Path(args.worktree))
        base_sha = await services.git.current_sha(location)
        rg_path = find_rg(settings)
        registry = build_default_registry(search_supports_regex=rg_path is not None).subset(
            ALL_TOOLS
        )

        async with session_scope(services.sessions) as session:
            repos = ScopedRepo(session, Repository, services.principal.workspace_id)
            repository = await repos.create(
                name=f"dev-run-task-claude-sdk-{uuid7()}",
                local_path=str(location.work_tree),
                created_by=services.principal.user_id,
            )
            tickets = ScopedRepo(session, Ticket, services.principal.workspace_id)
            ticket = await tickets.create(
                repo_id=repository.id,
                title="Dev run-task (claude_sdk)",
                created_by=services.principal.user_id,
            )
            runs = ScopedRepo(session, Run, services.principal.workspace_id)
            # A run's budget defaults to 0, which the gateway refuses on the
            # first call; a dev run gets the configured per-run budget.
            run = await runs.create(
                ticket_id=ticket.id,
                repo_id=repository.id,
                created_by=services.principal.user_id,
                budget_usd=Decimal(str(settings.run_budget_usd)),
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
                actor=f"dev:{task.agent_id or task.task_id}",
                approval_timeout_s=600,
                rg_path=rg_path,
            )

        task = AgentTask(
            task_id=uuid7(),
            run_id=run.id,
            run_step_id=None,
            workspace_id=services.principal.workspace_id,
            agent_id=None,
            role=AgentRole.LEAD,
            instructions=args.instructions,
            worktree=location,
            base_sha=base_sha,
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
        print(result.model_dump_json(indent=2))
        return 0


def _dev_run_task(args: argparse.Namespace) -> int:
    """Run one task through the real app lifespan (fake, native or claude_sdk)."""
    try:
        runtime = getattr(args, "runtime", "fake")
        if runtime == "native":
            return asyncio.run(_run_native_task(args, get_settings()))
        if runtime == "claude_sdk":
            return asyncio.run(_run_claude_sdk_task(args, get_settings()))
        return asyncio.run(_run_fake_task(args, get_settings()))
    except (OSError, ValueError, RuntimeError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1


def _dev(args: argparse.Namespace) -> int:
    if args.dev_command == "fake-run":
        return _dev_fake_run(args)
    if args.dev_command == "plan":
        return _dev_plan(args)
    if args.dev_command == "run-task":
        return _dev_run_task(args)
    return 2


_COMMANDS: dict[str, Callable[[argparse.Namespace], int]] = {
    "secrets": _secrets,
    "github": _github,
    "worktrees": _worktrees,
    "demo": _demo,
    "serve": _serve,
    "ui": _ui,
    "dev": _dev,
}


def _first_message(error: ValidationError) -> str:
    """Render pydantic's first complaint as one line, without pydantic's prefix.

    A :class:`pydantic.ValidationError` always carries at least one error.
    """
    return error.errors()[0]["msg"].removeprefix("Value error, ")


def main(argv: list[str] | None = None) -> int:
    """Run the CLI. Returns the process exit code."""
    parser = _build_parser()
    args = parser.parse_args(argv)

    try:
        settings = get_settings()
    except ValidationError as error:
        # Settings refuses an unsafe bind address or a non-positive budget (doc
        # 02). One line beats a traceback: this is a configuration mistake, and
        # the message says which variable to change.
        print(f"error: {_first_message(error)}", file=sys.stderr)
        return 2

    ensure_data_dir(settings)

    return _COMMANDS[args.command](args)
