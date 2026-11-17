"""A repository's allowed commands: what agents may run there without asking.

The owner registers argv prefixes per repository (``Repository.allowed_commands``).
They join the repository's test command in the command policy's ``extra_allow``,
so deny rules, the slashed-program rule and the bare-tripwire rule still apply.
Every entry is re-checked with :func:`allowed_entry_problem` when it is read, so a
hand-edited database row cannot widen the policy.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from gaia.db.engine import session_scope
from gaia.db.models import Approval, Repository, Run
from gaia.db.repo import ScopedRepo
from gaia.domain.enums import ApprovalKind, ApprovalStatus
from gaia.security.commands import (
    CommandPolicy,
    allowed_entry_problem,
    suggest_allowed_prefix,
)

Entry = tuple[str, ...]

#: Most past approvals read when suggesting allowed commands.
_SUGGESTION_SCAN_LIMIT = 1000


def allowed_entries(repository: Repository) -> tuple[Entry, ...]:
    """The repository's valid allowed commands, in registration order."""
    entries: list[Entry] = []
    for entry in repository.allowed_commands or ():
        if (
            isinstance(entry, list)
            and all(isinstance(part, str) for part in entry)
            and allowed_entry_problem(entry) is None
        ):
            entries.append(tuple(entry))
    return tuple(entries)


def extra_allow_for(repository: Repository) -> tuple[Entry, ...]:
    """The test command plus the allowed commands, for the policy's ``extra_allow``.

    The test command is only the owner's registration; a plan step's
    ``test_command`` is model output and is never added here.
    """
    test = (tuple(repository.test_command),) if repository.test_command else ()
    return (*test, *allowed_entries(repository))


async def load_extra_allow(
    sessions: async_sessionmaker[AsyncSession], workspace_id: str, run_id: str
) -> tuple[Entry, ...]:
    """Read ``extra_allow`` for a run's repository now, so a remembered command applies at once."""
    async with sessions() as session:
        run = await ScopedRepo(session, Run, workspace_id).get(run_id)
        if run is None:
            return ()
        repository = await ScopedRepo(session, Repository, workspace_id).get(run.repo_id)
        return () if repository is None else extra_allow_for(repository)


async def remember(
    sessions: async_sessionmaker[AsyncSession],
    workspace_id: str,
    repository_id: str,
    entry: Sequence[str],
) -> bool:
    """Append ``entry`` to the repository's allowed commands; ``False`` if already there.

    Committed in its own session, before the caller wakes the waiting agent, so
    the agent's next command already sees it.
    """
    problem = allowed_entry_problem(entry)
    if problem is not None:
        raise ValueError(problem)
    async with session_scope(sessions) as session:
        repositories = ScopedRepo(session, Repository, workspace_id)
        repository = await repositories.get(repository_id)
        if repository is None:
            raise LookupError(f"no repository {repository_id!r}")
        current = [list(existing) for existing in repository.allowed_commands or ()]
        if list(entry) in current:
            return False
        await repositories.update(repository_id, allowed_commands=[*current, list(entry)])
        return True


async def suggestions(
    session: AsyncSession, workspace_id: str, repository: Repository
) -> list[tuple[Entry, int]]:
    """Prefixes of commands the owner approved in this repository, not yet allowed.

    Most often approved first. A prefix that would already run without asking
    (an allowlisted tool, the test command or an allowed command) is left out.
    """
    rows = await session.execute(
        select(Approval.payload_json)
        .join(Run, Run.id == Approval.run_id)
        .where(
            Approval.workspace_id == workspace_id,
            Run.repo_id == repository.id,
            Approval.kind == ApprovalKind.COMMAND.value,
            Approval.status == ApprovalStatus.APPROVED.value,
        )
        .order_by(Approval.created_at.desc())
        .limit(_SUGGESTION_SCAN_LIMIT)
    )
    counts: Counter[Entry] = Counter()
    for (payload,) in rows:
        argv = payload.get("argv") if isinstance(payload, dict) else None
        if isinstance(argv, list):
            prefix = suggest_allowed_prefix(argv)
            if prefix is not None:
                counts[prefix] += 1
    policy = CommandPolicy.from_default()
    extra_allow = extra_allow_for(repository)
    return sorted(
        (
            (prefix, count)
            for prefix, count in counts.items()
            if policy.evaluate(prefix, extra_allow=extra_allow).action != "allow"
        ),
        key=lambda item: (-item[1], item[0]),
    )
