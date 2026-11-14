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
        help="run one fake agent task in a worktree (real runtimes arrive in 10b)",
    )
    run_task.add_argument("--runtime", choices=["fake"], default="fake", help="runtime to use")
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
    # TODO(gaia-16): add --open once the login flow says how a token may reach a
    # browser; a one-time code in the URL would keep it out of argv (doc 16).
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

    settings = get_settings()
    uvicorn.run(
        APP_TARGET,
        factory=True,
        host=settings.host,
        port=settings.port,
        reload=args.reload,
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


def _dev_run_task(args: argparse.Namespace) -> int:
    """Run one fake task through the real app lifespan."""
    try:
        return asyncio.run(_run_fake_task(args, get_settings()))
    except (OSError, ValueError) as error:
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
