"""Every git command Gaia runs, in a hardened environment (doc 05 task 2).

A checked-out branch is **untrusted content**. A repo can carry a ``pre-commit``
hook, a husky ``.husky/`` directory, a ``core.hooksPath`` of its own or a
``commit.gpgsign=true`` that pops a signing dialog - and every one of those runs
code when Gaia commits. So no git call leaves this module without:

- ``GIT_CONFIG_NOSYSTEM=1``: ``/etc/gitconfig`` is not read;
- ``GIT_CONFIG_GLOBAL=<data_dir>/gitconfig``: Gaia's own config (``user.name``,
  ``user.email``, ``commit.gpgsign=false``) replaces the owner's;
- ``-c core.hooksPath=/dev/null`` on the command line, which beats the repo's
  own config, so no hook in the repo runs - including a husky ``.husky/`` that an
  agent could have written;
- ``-c commit.gpgsign=false``, because signing would spawn a pinentry program
  (and fail with no terminal);
- ``-c core.pager=cat``, so nothing can decide to invoke a pager on us;
- ``GIT_TERMINAL_PROMPT=0`` (from :mod:`gaia.services.proc`), so a private
  remote fails instead of waiting for a password;
- ``--no-verify`` on commits, the belt to the ``core.hooksPath`` braces.

The environment is built once, in :meth:`GitService.__init__`, and the config
file is written there too: a run's git behaviour cannot change under it, and a
test that points ``data_dir`` at ``tmp_path`` gets its own git world.

Everything is spawned through :func:`gaia.services.proc.run_proc`. Nothing here
interpolates a shell, and every ref or path is checked before it becomes an
argument (a ref starting with ``-`` would be read as an option).
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import structlog

from gaia.config.settings import Settings
from gaia.security.redaction import redact
from gaia.services.proc import DEFAULT_MAX_OUTPUT_BYTES, DEFAULT_TIMEOUT_S, ProcResult, run_proc

logger = structlog.get_logger(__name__)

#: The binary. Found through ``PATH``, which ``run_proc`` gives the child.
GIT = "git"

#: The identity every commit is authored with, when the caller does not override
#: it (doc 09b passes ``-c user.name=<agent>`` later on the command line).
GAIA_NAME = "Gaia"
GAIA_EMAIL = "gaia@localhost"

#: Appended to every invocation, before the subcommand. ``-c`` beats both the
#: repo's ``.git/config`` and any config file, which is the point.
HARDENED_CONFIG: tuple[str, ...] = (
    "-c",
    "core.hooksPath=/dev/null",
    "-c",
    "core.fsmonitor=false",
    "-c",
    "commit.gpgsign=false",
    "-c",
    "core.pager=cat",
    # Parsers read this output: no ANSI escapes, and no octal-escaped paths for
    # filenames with non-ASCII characters in them.
    "-c",
    "color.ui=never",
    "-c",
    "core.quotepath=false",
    # The identity is pinned here as well as in the git config file, because a
    # repo-local ``user.name`` beats ``GIT_CONFIG_GLOBAL`` and a repo's content is
    # untrusted. A later ``-c`` on one call still wins, which is how doc 09b
    # attributes a commit to the agent that made it.
    "-c",
    f"user.name={GAIA_NAME}",
    "-c",
    f"user.email={GAIA_EMAIL}",
)

#: Gaia's git config, written under ``data_dir`` (never the owner's).
GITCONFIG_NAME = "gitconfig"

#: Contents of that file. ``defaultBranch`` only affects a repo Gaia initialises,
#: which is not yet a thing, but it documents the branch name used everywhere.
GITCONFIG_CONTENT = f"""\
# Written by Gaia on every start (doc 05). Do not edit: a repo's content never
# configures Gaia's git, and neither does the owner's global config.
[user]
\tname = {GAIA_NAME}
\temail = {GAIA_EMAIL}
[commit]
\tgpgsign = false
[init]
\tdefaultBranch = main
[core]
\thooksPath = /dev/null
\tpager = cat
"""

#: Branch names Gaia creates begin with this (ARCHITECTURE 6.6: agents push only
#: to ``gaia/*`` branches).
BRANCH_PREFIX = "gaia/"

#: The doc's cap on the slug half of a branch name.
SLUG_MAX_LENGTH = 40

#: Used when a title has nothing sluggable in it at all.
SLUG_FALLBACK = "work"

#: Hex-ish id characters, collapsed out of the ticket id.
_NON_ID_CHARS = re.compile(r"[^0-9a-zA-Z]")
#: Everything that is not a slug character.
_NON_SLUG_CHARS = re.compile(r"[^a-z0-9]+")
#: A value git may see as a revision, a branch or a worktree name.
_REF_FORBIDDEN = re.compile(r"[\x00-\x20\x7f]")


class GitError(RuntimeError):
    """A git command failed, or could not be run.

    The message carries the subcommand, the exit status (or the timeout) and the
    first line of stderr, passed through :func:`gaia.security.redaction.redact`:
    stderr is where a remote URL with a token ends up (doc 15).
    """

    def __init__(self, args: tuple[str, ...], result: ProcResult) -> None:
        self.git_args = args
        self.result = result
        # A single line, capped: a git failure can be a wall of text.
        detail = _first_line(result)[:500]
        message = f"git {' '.join(args)} failed: {result.describe()}"
        # Redacted as a whole, not just the stderr: the argv is the other place a
        # token turns up (doc 15 pushes with one), and an error message ends up in
        # a log.
        super().__init__(redact(f"{message}: {detail}" if detail else message))


class NotARepository(ValueError):
    """``validate_repo`` was given a path that is not the root of a git repo.

    Not a :class:`GitError`: nothing failed to run, the answer is simply "no".
    ``reason`` is git's own stderr line when git was the one that said no.
    """

    def __init__(self, path: Path, reason: str) -> None:
        self.path = path
        self.reason = reason
        super().__init__(f"{str(path)!r} is not a git repository root: {reason}")


class InvalidRef(ValueError):
    """A branch or revision name Gaia refuses to hand to git."""


class UnsafePath(ValueError):
    """A path resolved outside the roots Gaia allows (doc 05 task 4)."""


class WorktreeTampered(UnsafePath):
    """A worktree's ``.git`` file no longer matches its recorded admin dir.

    Raised before a git command runs with a :class:`WorktreeLocation`: the pin is
    what stops a planted ``gitdir:`` file from pointing Gaia at attacker config
    (review finding F1).
    """


@dataclass(frozen=True)
class WorktreeLocation:
    """Where a git command inside a worktree is allowed to run.

    Both paths are resolved. ``work_tree`` is the checkout and ``git_dir`` is the
    administrative directory under the trusted repository's ``.git/worktrees``.
    A caller must pass this object rather than a bare path once a path is inside
    ``worktrees_root``, so a worktree call can never forget the F1 pin.
    """

    work_tree: Path
    git_dir: Path

    def __post_init__(self) -> None:
        object.__setattr__(self, "work_tree", _resolved(self.work_tree))
        object.__setattr__(self, "git_dir", _resolved(self.git_dir))


@dataclass(frozen=True)
class Commit:
    """One entry of ``git log``."""

    sha: str
    summary: str


@dataclass(frozen=True)
class GitWorktree:
    """One block of ``git worktree list --porcelain``.

    ``branch`` is the short name (``gaia/x``), not ``refs/heads/gaia/x``, because
    that is what the ``worktree`` row stores.
    """

    path: Path
    head: str | None
    branch: str | None
    bare: bool
    detached: bool
    prunable: bool


class TicketLike(Protocol):
    """What :meth:`GitService.branch_name_for` needs: an id and a title."""

    id: str
    title: str


def resolve_within(root: Path, path: Path | str) -> Path:
    """Resolve ``path`` and require it to stay under ``root`` (task 4).

    ``resolve()`` follows symlinks first, so a link inside the root that points
    outside it is refused rather than trusted. Both sides are resolved, because
    ``tmp_path`` on macOS is ``/var/...`` while ``resolve()`` says
    ``/private/var/...``.

    TODO(gaia-09a): replace with ``security.paths.confine()`` when 09a lands; this
    is the same check, but it belongs with the sandbox profile and the tool path
    policy rather than in the git service.
    """
    resolved_root = Path(root).resolve()
    resolved = Path(path).resolve()
    if not resolved.is_relative_to(resolved_root):
        raise UnsafePath(f"{str(resolved)!r} is outside {str(resolved_root)!r}")
    return resolved


def short_id(row_id: str) -> str:
    """The random tail of an id, for a branch name.

    The **last** eight characters, not the first: a UUIDv7 starts with the
    timestamp, so the first eight characters of two tickets created in the same
    millisecond are identical. The tail is random.
    """
    compact = _NON_ID_CHARS.sub("", row_id).lower()
    if not compact:
        raise InvalidRef(f"{row_id!r} has no characters usable in a branch name")
    return compact[-8:]


def slugify(title: str) -> str:
    """A ``[a-z0-9-]`` slug of at most :data:`SLUG_MAX_LENGTH` characters.

    Accents are folded rather than dropped (``Café`` becomes ``cafe``), because a
    branch named after a ticket should look like the ticket. Returns ``""`` for a
    title with nothing sluggable in it; the caller picks a fallback.
    """
    folded = unicodedata.normalize("NFKD", title).encode("ascii", "ignore").decode("ascii")
    slug = _NON_SLUG_CHARS.sub("-", folded.lower()).strip("-")
    return slug[:SLUG_MAX_LENGTH].rstrip("-")


def check_ref_argument(name: str, *, what: str = "ref") -> str:
    """Refuse a name git would misread as an option or split on whitespace.

    Only the argv-safety half of validation: a range like ``main..HEAD`` is a
    legitimate revision argument, so ``..`` is allowed here and rejected by
    :meth:`GitService.validate_ref`, which asks git itself.
    """
    if not name:
        raise InvalidRef(f"a {what} name cannot be empty")
    if name.startswith("-"):
        raise InvalidRef(f"{name!r} starts with '-', which git would read as an option")
    if _REF_FORBIDDEN.search(name):
        raise InvalidRef(f"{name!r} contains whitespace or a control character")
    return name


def _check_worktree_location(location: WorktreeLocation) -> None:
    """Refuse a worktree whose ``.git`` file has been tampered with.

    The whole F1 defence is that git never reads this file; the check is here so
    a tampered worktree fails loudly before a command runs instead of silently
    running against the trusted admin dir while the worktree points elsewhere.
    """
    git_file = location.work_tree / ".git"
    if git_file.is_symlink() or not git_file.is_file():
        raise WorktreeTampered(
            f"worktree .git entry {str(git_file)!r} is not a regular file; refusing to run git"
        )
    expected = f"gitdir: {location.git_dir}"
    content = git_file.read_text()
    if content != expected and content != f"{expected}\n":
        raise WorktreeTampered(
            f"worktree .git entry {str(git_file)!r} does not point at "
            f"{str(location.git_dir)!r}; refusing to run git"
        )


def _resolve_from(base: Path, value: str) -> Path:
    """Resolve a git-reported path, relative to ``base`` when it is relative."""
    candidate = Path(value)
    if not candidate.is_absolute():
        candidate = base / candidate
    return _resolved(candidate)


def _scan_worktree_admin_dirs(common_dir: Path, expected_git_file: Path) -> Path | None:
    """Return the admin dir whose ``gitdir`` entry resolves to ``expected_git_file``.

    Only the repository side is read. The worktree's own ``.git`` file is
    deliberately never consulted here (F1): an agent can rewrite it, but it
    cannot rewrite the trusted repository's ``.git/worktrees/*/gitdir`` entries.
    """
    worktrees_dir = common_dir / "worktrees"
    if not worktrees_dir.is_dir():
        return None
    for gitdir_file in sorted(worktrees_dir.glob("*/gitdir")):
        try:
            raw = gitdir_file.read_text().strip()
        except OSError:
            continue
        candidate = Path(raw)
        if not candidate.is_absolute():
            # git writes an absolute path today; resolve a future relative value
            # from the admin directory that contains the gitdir file.
            candidate = gitdir_file.parent / candidate
        if _resolved(candidate) == expected_git_file:
            return gitdir_file.parent
    return None


class GitService:
    """Gaia's only door to git.

    Constructed once per process from :class:`~gaia.config.settings.Settings`,
    which supplies ``data_dir`` (where the git config goes) and
    ``worktrees_root`` (the only place a worktree may live).

    ``timeout_s`` bounds every command. Each command method also takes
    ``timeout_s=`` to override it for one call, because the right bound is a
    property of the command rather than of the service: a ``status`` on a request
    path wants seconds, a ``push`` of a large repo (doc 15) wants minutes.
    """

    def __init__(self, settings: Settings, *, timeout_s: float = DEFAULT_TIMEOUT_S) -> None:
        self.data_dir = Path(settings.data_dir)
        self.worktrees_root = Path(settings.worktrees_root)
        self.timeout_s = timeout_s
        self.config_path = self.data_dir / GITCONFIG_NAME
        self._write_config()

    def _write_config(self) -> None:
        """Write Gaia's git config under ``data_dir``, owner-readable only.

        Written on construction rather than per call: git reads it on every
        invocation anyway, and rewriting it mid-run would let one call see a
        different identity than the next.
        """
        self.data_dir.mkdir(parents=True, exist_ok=True)
        if not self.config_path.exists() or self.config_path.read_text() != GITCONFIG_CONTENT:
            self.config_path.write_text(GITCONFIG_CONTENT)
            self.config_path.chmod(0o600)

    def _env(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        """The environment every git call gets, plus ``extra`` for one call."""
        env = {
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": str(self.config_path),
            "GIT_TERMINAL_PROMPT": "0",
        }
        if extra:
            env.update(extra)
        return env

    async def _invoke(
        self,
        *args: str,
        cwd: Path,
        timeout_s: float | None = None,
        env: dict[str, str] | None = None,
        max_output_bytes: int = DEFAULT_MAX_OUTPUT_BYTES,
    ) -> ProcResult:
        """Run one git command and return the raw result, failed or not."""
        argv = [GIT, *HARDENED_CONFIG, *args]
        return await run_proc(
            argv,
            cwd=Path(cwd),
            timeout_s=self.timeout_s if timeout_s is None else timeout_s,
            env=self._env(env),
            max_output_bytes=max_output_bytes,
        )

    async def _run(
        self,
        *args: str,
        cwd: Path,
        timeout_s: float | None = None,
        env: dict[str, str] | None = None,
        max_output_bytes: int = DEFAULT_MAX_OUTPUT_BYTES,
    ) -> ProcResult:
        """Run one git command, raising :class:`GitError` unless it succeeded."""
        result = await self._invoke(
            *args, cwd=cwd, timeout_s=timeout_s, env=env, max_output_bytes=max_output_bytes
        )
        if not result.ok:
            raise GitError(args, result)
        return result

    # ---------------------------------------------------------------- repos --
    async def validate_repo(self, path: Path | str, *, timeout_s: float | None = None) -> Path:
        """Return the repository root for ``path``, or raise.

        Refused: a path that is not a directory, a directory outside git, and
        anything inside ``worktrees_root`` - a worktree is a checkout of a repo,
        not a repo to register, and registering one would let a run nest
        worktrees inside itself (task 2).
        """
        candidate = _resolved(path)
        if not candidate.is_dir():
            raise NotARepository(candidate, "it is not a directory")
        self._refuse_worktree_path(candidate)
        result = await self._invoke(
            "rev-parse", "--show-toplevel", cwd=candidate, timeout_s=timeout_s
        )
        if not result.ok:
            raise NotARepository(candidate, _first_line(result) or result.describe())
        root = _resolved(result.stdout.strip())
        self._refuse_worktree_path(root)
        return root

    def _refuse_worktree_path(self, path: Path) -> None:
        """Raise if ``path`` is inside ``worktrees_root``."""
        if path.is_relative_to(self.worktrees_root.resolve()):
            raise UnsafePath(
                f"{str(path)!r} is inside the worktrees root; register the repository itself"
            )

    def _command_context(self, repo: Path | WorktreeLocation) -> tuple[Path, dict[str, str] | None]:
        """Return the ``cwd`` and env for a git command from ``repo``.

        A bare path inside ``worktrees_root`` is refused: the caller must pass a
        :class:`WorktreeLocation` so the F1 pin cannot be forgotten. A worktree
        location is checked before every command and then run with ``GIT_DIR``
        and ``GIT_WORK_TREE`` set from the trusted repository side.
        """
        if isinstance(repo, WorktreeLocation):
            _check_worktree_location(repo)
            return repo.work_tree, {
                "GIT_DIR": str(repo.git_dir),
                "GIT_WORK_TREE": str(repo.work_tree),
            }
        path = _resolved(repo)
        if path.is_relative_to(self.worktrees_root.resolve()):
            raise UnsafePath(f"{str(path)!r} is inside the worktrees root; pass a WorktreeLocation")
        return path, None

    def verify_location(self, location: WorktreeLocation) -> None:
        """Raise :class:`WorktreeTampered` unless ``location``'s ``.git`` file is intact.

        For callers that hand a worktree path to a git verb that runs on the
        repository (``git worktree remove``), where no pinned env applies.
        """
        _check_worktree_location(location)

    async def worktree_admin_dir(
        self, repo: Path, work_tree: Path, *, timeout_s: float | None = None
    ) -> Path:
        """Return the trusted admin dir for ``work_tree`` under ``repo``.

        Asks the **repository** (which Gaia registered and trusts) for its
        ``git-common-dir``, then scans ``<common>/worktrees/*/gitdir`` for the
        entry that resolves to ``<work_tree>/.git``. The worktree's own ``.git``
        file is never read here: an agent can rewrite it (F1).
        """
        repo_path = _resolved(repo)
        self._refuse_worktree_path(repo_path)
        work_tree_path = _resolved(work_tree)
        result = await self._run(
            "rev-parse", "--git-common-dir", cwd=repo_path, timeout_s=timeout_s
        )
        common_dir = _resolve_from(repo_path, result.stdout.strip())
        admin_dir = _scan_worktree_admin_dirs(common_dir, work_tree_path / ".git")
        if admin_dir is None:
            raise UnsafePath(
                f"no worktree admin dir under {str(common_dir)!r} points at "
                f"{str(work_tree_path / '.git')!r}"
            )
        return admin_dir

    async def current_sha(
        self, repo: Path | WorktreeLocation, rev: str = "HEAD", *, timeout_s: float | None = None
    ) -> str:
        """The commit ``rev`` points at, as a full SHA."""
        check_ref_argument(rev, what="revision")
        cwd, env = self._command_context(repo)
        result = await self._run(
            "rev-parse", "--verify", f"{rev}^{{commit}}", cwd=cwd, env=env, timeout_s=timeout_s
        )
        return result.stdout.strip()

    async def current_branch(
        self, repo: Path | WorktreeLocation, *, timeout_s: float | None = None
    ) -> str | None:
        """The checked-out branch, or ``None`` for a detached HEAD."""
        cwd, env = self._command_context(repo)
        result = await self._invoke(
            "symbolic-ref", "--quiet", "--short", "HEAD", cwd=cwd, env=env, timeout_s=timeout_s
        )
        return result.stdout.strip() or None

    async def default_branch(self, repo: Path, *, timeout_s: float | None = None) -> str:
        """The branch a run should start from.

        ``origin/HEAD`` when the repo has a remote that names it - that is the
        repo owner's answer, not ours. Otherwise the checked-out branch (a local
        repo with no remote), and ``main`` as the last resort. Doc 06 lets the
        owner set ``repository.default_branch`` for anything stranger.
        """
        remote_head = await self._invoke(
            "symbolic-ref",
            "--quiet",
            "--short",
            "refs/remotes/origin/HEAD",
            cwd=repo,
            timeout_s=timeout_s,
        )
        if remote_head.ok and remote_head.stdout.strip():
            return remote_head.stdout.strip().removeprefix("origin/")
        return await self.current_branch(repo, timeout_s=timeout_s) or "main"

    async def status_porcelain(
        self, repo: Path | WorktreeLocation, *, timeout_s: float | None = None
    ) -> str:
        """``git status --porcelain`` verbatim: empty when the tree is clean."""
        cwd, env = self._command_context(repo)
        result = await self._run("status", "--porcelain", cwd=cwd, env=env, timeout_s=timeout_s)
        return result.stdout

    async def is_dirty(
        self, repo: Path | WorktreeLocation, *, timeout_s: float | None = None
    ) -> bool:
        """True when the tree has staged, unstaged or untracked changes."""
        return bool((await self.status_porcelain(repo, timeout_s=timeout_s)).strip())

    async def diff(
        self,
        repo: Path | WorktreeLocation,
        base: str,
        head: str,
        *,
        timeout_s: float | None = None,
        max_output_bytes: int = DEFAULT_MAX_OUTPUT_BYTES,
    ) -> str:
        """``git diff base head``, capped at ``max_output_bytes``.

        Two-dot: the literal difference between the two commits, which is what a
        review of a finished branch wants. A merge-base diff is one call away
        (``diff(repo, base, f"{base}...{head}")``) and doc 14 decides which.

        ``--no-ext-diff --no-textconv`` are defence in depth: external diff and
        textconv drivers are other exec-capable config keys, and Gaia's own diff
        output must never run a repo's helper.
        """
        check_ref_argument(base, what="revision")
        check_ref_argument(head, what="revision")
        cwd, env = self._command_context(repo)
        result = await self._run(
            "diff",
            "--no-ext-diff",
            "--no-textconv",
            base,
            head,
            cwd=cwd,
            env=env,
            timeout_s=timeout_s,
            max_output_bytes=max_output_bytes,
        )
        return result.stdout

    async def log(
        self,
        repo: Path | WorktreeLocation,
        *,
        limit: int = 20,
        rev: str = "HEAD",
        timeout_s: float | None = None,
    ) -> list[Commit]:
        """The newest ``limit`` commits reachable from ``rev``, newest first."""
        if limit <= 0:
            raise ValueError(f"limit must be positive, not {limit!r}")
        check_ref_argument(rev, what="revision")
        cwd, env = self._command_context(repo)
        result = await self._run(
            "log",
            f"--max-count={limit}",
            "--pretty=format:%H%x1f%s%x1e",
            rev,
            cwd=cwd,
            env=env,
            timeout_s=timeout_s,
        )
        commits = []
        for record in result.stdout.split("\x1e"):
            sha, _, summary = record.strip().partition("\x1f")
            if sha:
                commits.append(Commit(sha=sha, summary=summary))
        return commits

    async def commit(
        self,
        repo: Path | WorktreeLocation,
        message: str,
        *,
        add_all: bool = True,
        timeout_s: float | None = None,
    ) -> str:
        """Stage (optionally) and commit; return the new SHA.

        ``--no-verify`` is the belt to ``core.hooksPath=/dev/null``'s braces: a
        repo hook is code the agent may have written, and Gaia does not run agent
        code it did not ask for (the doc's hook test, ARCHITECTURE 6.11). Doc
        09b's ``git_commit`` tool is the caller.

        TODO(gaia-15): ``push_branch`` belongs beside this, with the token in the
        child's environment rather than in argv; doc 15 owns the remote. It will
        pass a larger ``timeout_s`` than the 30 s default.
        """
        if not message.strip():
            raise ValueError("a commit message cannot be empty")
        cwd, env = self._command_context(repo)
        if add_all:
            await self._run("add", "--all", cwd=cwd, env=env, timeout_s=timeout_s)
        await self._run(
            "commit", "--no-verify", "--message", message, cwd=cwd, env=env, timeout_s=timeout_s
        )
        return await self.current_sha(repo, timeout_s=timeout_s)

    # -------------------------------------------------------------- branches --
    def branch_name_for(self, ticket: TicketLike) -> str:
        """``gaia/<short-id>-<slug>`` for a ticket (task 2)."""
        slug = slugify(ticket.title) or SLUG_FALLBACK
        return f"{BRANCH_PREFIX}{short_id(ticket.id)}-{slug}"

    async def validate_ref(self, name: str, *, timeout_s: float | None = None) -> str:
        """Ask git whether ``name`` is a legal branch name; return it if so.

        ``git check-ref-format --branch`` is the authority, not a regex: it knows
        about ``..``, ``@{``, a trailing ``.lock``, a leading ``-`` and the rest.
        """
        check_ref_argument(name, what="branch")
        result = await self._invoke(
            "check-ref-format", "--branch", name, cwd=self.data_dir, timeout_s=timeout_s
        )
        if not result.ok:
            raise InvalidRef(f"{name!r} is not a valid branch name")
        return name

    async def branch_exists(
        self, repo: Path, branch: str, *, timeout_s: float | None = None
    ) -> bool:
        """True when ``refs/heads/<branch>`` exists."""
        check_ref_argument(branch, what="branch")
        result = await self._invoke(
            "rev-parse",
            "--verify",
            "--quiet",
            f"refs/heads/{branch}",
            cwd=repo,
            timeout_s=timeout_s,
        )
        return result.ok

    # ------------------------------------------------------------- worktrees --
    async def worktree_list(
        self, repo: Path, *, timeout_s: float | None = None
    ) -> list[GitWorktree]:
        """``git worktree list --porcelain``, parsed (task 3)."""
        result = await self._run("worktree", "list", "--porcelain", cwd=repo, timeout_s=timeout_s)
        return _parse_worktree_porcelain(result.stdout)

    async def worktree_add(
        self,
        repo: Path,
        *,
        path: Path,
        ref: str,
        new_branch: str | None = None,
        timeout_s: float | None = None,
    ) -> None:
        """``git worktree add``: check out ``ref``, optionally on a new branch.

        ``new_branch`` creates that branch at ``ref`` (``-b``); without it the
        worktree checks ``ref`` out, which git refuses when another worktree
        already has it - the reuse case for a resumed run.

        ``path`` must already be resolved and confined; that policy belongs to
        :class:`~gaia.services.worktrees.WorktreeService`. ``--`` ends the
        options, so a path can never be read as a flag.
        """
        check_ref_argument(ref, what="revision")
        args = ["worktree", "add"]
        if new_branch is not None:
            check_ref_argument(new_branch, what="branch")
            args += ["-b", new_branch]
        _check_absolute(path)
        await self._run(*args, "--", str(path), ref, cwd=repo, timeout_s=timeout_s)

    async def worktree_remove(
        self, repo: Path, path: Path, *, force: bool = False, timeout_s: float | None = None
    ) -> None:
        """Remove the worktree at ``path``; ``force`` discards local changes."""
        _check_absolute(path)
        args = ["worktree", "remove"]
        if force:
            args.append("--force")
        await self._run(*args, "--", str(path), cwd=repo, timeout_s=timeout_s)

    async def worktree_prune(self, repo: Path, *, timeout_s: float | None = None) -> None:
        """Drop git's records of worktrees whose directory is gone."""
        await self._run("worktree", "prune", cwd=repo, timeout_s=timeout_s)


def _check_absolute(path: Path) -> None:
    """A relative worktree path would land wherever the process happens to be."""
    if not Path(path).is_absolute():
        raise UnsafePath(f"worktree path {str(path)!r} must be absolute")


def _resolved(path: Path | str) -> Path:
    """``Path.resolve()``, outside the coroutine: ruff's ASYNC240 wants blocking
    path calls in plain functions."""
    return Path(path).resolve()


def _first_line(result: ProcResult) -> str:
    """The first line of stderr, redacted: git's own explanation, in one line."""
    lines = result.stderr.strip().splitlines()
    return redact(lines[0]) if lines else ""


def _parse_worktree_porcelain(output: str) -> list[GitWorktree]:
    """Turn ``git worktree list --porcelain`` into :class:`GitWorktree` values.

    The format is blank-line separated blocks of ``key value`` lines, as
    ``git-worktree(1)`` documents. Unknown keys are ignored rather than fatal: git
    adds one now and then, and a new key is no reason to stop listing.
    """
    worktrees: list[GitWorktree] = []
    for block in output.split("\n\n"):
        fields: dict[str, str] = {}
        flags: set[str] = set()
        for line in block.splitlines():
            key, _, value = line.partition(" ")
            if value:
                fields[key] = value
            else:
                flags.add(key)
        if "worktree" not in fields:
            continue
        ref = fields.get("branch")
        worktrees.append(
            GitWorktree(
                path=Path(fields["worktree"]),
                head=fields.get("HEAD"),
                branch=ref.removeprefix("refs/heads/") if ref else None,
                bare="bare" in flags,
                detached="detached" in flags,
                prunable="prunable" in flags,
            )
        )
    return worktrees
