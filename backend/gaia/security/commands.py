"""Command policy for agent-spawned processes (doc 09a task 2).

The policy is a **tripwire, not a boundary**: allowlisted test runners execute
code the agent wrote, and containment comes from the OS sandbox built by
:mod:`gaia.security.sandbox`. This module only decides ``allow``, ``deny`` or
``ask`` for an argv list, with a reason string the audit log can record.

The default allow rules live in ``command_policy.default.toml`` and are loaded
through Pydantic models with ``extra="forbid"`` so an unexpected key fails at
load time rather than being ignored.
"""

from __future__ import annotations

import re
import tomllib
from collections.abc import Sequence
from pathlib import Path
from typing import cast

from pydantic import BaseModel, ConfigDict, Field

from gaia.security.decision import Decision

DEFAULT_POLICY_PATH = Path(__file__).with_name("command_policy.default.toml")

#: Hard limits from the hand-off. Anything outside them is ``deny``.
MAX_ARGV_ITEMS = 256
MAX_ARG_LENGTH = 4096

#: ``uv run`` flags that may appear before the inner command. Anything else
#: (``--with``, ``--project``, ...) changes what runs and therefore gets ask.
_UV_RUN_FLAGS = frozenset({"--frozen", "--offline", "--no-sync", "--quiet", "-q"})

#: How many wrappers may be peeled off one argv (``uv run python -m pytest`` is
#: two). Anything nested deeper is unusual enough to ask about.
MAX_UNWRAP_DEPTH = 4

#: Tools that are always denied, grouped by the reason shown to the user.
_DENY_RULES: tuple[tuple[tuple[str, ...], str], ...] = (
    (("sudo", "sudoedit", "su", "doas", "pkexec"), "privilege escalation"),
    (
        (
            "curl",
            "wget",
            "ssh",
            "scp",
            "sftp",
            "rsync",
            "nc",
            "ncat",
            "netcat",
            "telnet",
            "ftp",
            "socat",
        ),
        "network",
    ),
    (("rm", "rmdir", "dd", "mkfs", "shred", "diskutil"), "destructive"),
    (
        (
            "open",
            "osascript",
            "security",
            "launchctl",
            "defaults",
            "pbcopy",
            "pbpaste",
            "screencapture",
        ),
        "macOS escape",
    ),
    (("crontab", "at"), "persistence"),
    (("chown",), "ownership"),
    (("kill", "pkill", "killall"), "process control"),
)

#: ``chmod`` is denied only for the setuid/setgid spellings named by the doc.
_SETUID_CHMOD_FLAGS = ("+s", "u+s")

#: A numeric mode that sets setuid, setgid or sticky bits (``4755``, ``02755``).
_SETUID_OCTAL = re.compile(r"0?[1-7][0-7]{3}")

#: Shells and process wrappers that must never run without approval.
_SHELL_WRAPPERS = frozenset(
    {
        "bash",
        "sh",
        "zsh",
        "fish",
        "dash",
        "ksh",
        "env",
        "xargs",
        "exec",
        "eval",
        "nohup",
        "timeout",
        "nice",
        "time",
    }
)

#: Interpreters that must never run without approval, whatever their arguments.
_INTERPRETERS = frozenset({"perl", "ruby", "php", "deno", "bun"})

#: Package managers that can install software or reach the network.
_PACKAGE_MANAGERS = frozenset({"uv", "npm", "npx", "pip"})


class AllowRule(BaseModel):
    """One ``[[allow]]`` entry from the TOML policy file."""

    model_config = ConfigDict(extra="forbid")

    cmd: str
    subcommands: tuple[str, ...] = ()
    deny_flags: tuple[str, ...] = ()


class CommandPolicyFile(BaseModel):
    """The TOML file's root shape.

    The file deliberately contains only allow rules. Deny and ask rules are
    code, not configuration: they must not be editable by a repo or by an
    accidental TOML edit.
    """

    model_config = ConfigDict(extra="forbid")

    allow: tuple[AllowRule, ...] = Field(default_factory=tuple)


class CommandPolicy:
    """The loaded command policy."""

    def __init__(self, allow_rules: tuple[AllowRule, ...]) -> None:
        self.allow_rules = allow_rules

    @classmethod
    def from_toml(cls, path: Path) -> CommandPolicy:
        """Load a policy file, failing closed on malformed input.

        Both TOML syntax errors and Pydantic validation errors propagate: a
        malformed policy must never silently become an empty allowlist or a
        permissive default.
        """
        raw = Path(path).read_text(encoding="utf-8")
        data = tomllib.loads(raw)
        parsed = CommandPolicyFile.model_validate(data)
        return cls(parsed.allow)

    @classmethod
    def from_default(cls) -> CommandPolicy:
        """Load the policy that ships with Gaia."""
        return cls.from_toml(DEFAULT_POLICY_PATH)

    def evaluate(
        self,
        argv: Sequence[str],
        *,
        extra_allow: Sequence[Sequence[str]] = (),
    ) -> Decision:
        """Decide what may happen for ``argv``.

        ``extra_allow`` is the owner-registered per-repo test command (D3). It
        can only upgrade ``ask`` to ``allow``; deny and the shell/interpreter
        tripwires always win.
        """
        invalid = _invalid_argv_decision(argv)
        if invalid is not None:
            return invalid

        command = _unwrap_all(tuple(argv))
        if isinstance(command, Decision):
            return command
        return self._evaluate_command(command, extra_allow)

    def _evaluate_command(
        self, command: tuple[str, ...], extra_allow: Sequence[Sequence[str]]
    ) -> Decision:
        """Evaluate one concrete command (after wrapper unwrapping)."""
        program = command[0]
        basename = _basename(program)

        denied = _deny_decision(basename, command)
        if denied is not None:
            return denied

        # A program named with a slash is a path the agent could have written
        # (``./pytest``). It is never eligible for an allow rule or extra_allow.
        if "/" in program:
            return Decision("ask", f"ask: command path {program!r} is not allowlisted")

        # The owner-registered test command (D3) is checked before the
        # package-manager and interpreter tripwires: registering ``npm test`` or
        # ``bash scripts/test.sh`` is the owner's explicit decision, and a
        # tripwire that overrode it would make the registration useless. Deny
        # rules and slashed programs (above) still win.
        extra = _extra_allow_decision(command, extra_allow)
        if extra is not None:
            return extra

        if basename in _PACKAGE_MANAGERS:
            return Decision("ask", f"ask: package manager {basename!r} requires approval")

        asked = _ask_decision(basename, command)
        if asked is not None:
            return asked

        for rule in self.allow_rules:
            allowed = _allow_decision(rule, basename, command)
            if allowed is not None:
                return allowed

        return Decision("ask", f"ask: no rule matched {basename!r}")


def _invalid_argv_decision(argv: Sequence[object]) -> Decision | None:
    """Return a deny decision when ``argv`` is not a sane argv list.

    The parameter is ``Sequence[object]`` on purpose: callers pass typed lists,
    but this is a security boundary and must also handle a caller that ignored
    the type checker.
    """
    if isinstance(argv, str):
        return Decision("deny", "deny: invalid argv: expected a list of arguments, not a string")
    if not argv:
        return Decision("deny", "deny: invalid argv: argv is empty")
    if len(argv) > MAX_ARGV_ITEMS:
        return Decision("deny", f"deny: invalid argv: at most {MAX_ARGV_ITEMS} arguments")
    for argument in argv:
        if not isinstance(argument, str):
            return Decision("deny", "deny: invalid argv: every argument must be a str")
        if len(argument) > MAX_ARG_LENGTH:
            return Decision(
                "deny",
                f"deny: invalid argv: each argument is at most {MAX_ARG_LENGTH} characters",
            )
        if "\x00" in argument:
            return Decision("deny", "deny: invalid argv: NUL byte in argument")
    # Every argument is str after the loop above; the cast keeps mypy aware of
    # the runtime check without adding a line that can never execute.
    program = cast("str", argv[0])
    if "\n" in program:
        return Decision("deny", "deny: invalid argv: newline in program name")
    return None


def _basename(program: str) -> str:
    """The final path component, so deny rules catch ``/usr/bin/git``."""
    return program.rsplit("/", 1)[-1]


def _deny_decision(basename: str, command: tuple[str, ...]) -> Decision | None:
    """Apply the hard deny list. Deny always wins, even over extra_allow."""
    if basename == "git":
        return Decision(
            "deny",
            "deny: use the git tools (09b), which run through GitService's hardened environment",
        )
    if basename == "chmod" and _has_setuid_flag(command[1:]):
        return Decision("deny", "deny: chmod with setuid/setgid flags")
    for commands, label in _DENY_RULES:
        if basename in commands:
            return Decision("deny", f"deny: {label} tool {basename!r}")
    return None


def _has_setuid_flag(args: tuple[str, ...]) -> bool:
    """True for symbolic setuid spellings (``+s``, ``u+s``, ``g+sx``) and numeric modes.

    A four-digit octal mode whose first digit is non-zero sets setuid, setgid
    or the sticky bit (``4755``, ``2755``, ``01777``).
    """
    return any(
        _SETUID_OCTAL.fullmatch(arg) is not None or any(flag in arg for flag in _SETUID_CHMOD_FLAGS)
        for arg in args
    )


def _ask_decision(basename: str, command: tuple[str, ...]) -> Decision | None:
    """Apply the shell/interpreter tripwire before any allow rule."""
    if basename in _SHELL_WRAPPERS:
        return Decision("ask", f"ask: shell or process wrapper {basename!r}")
    if basename in ("python", "python3"):
        return Decision("ask", f"ask: interpreter {basename!r}")
    if basename in _INTERPRETERS:
        return Decision("ask", f"ask: interpreter {basename!r}")
    if basename == "node" and len(command) > 1 and command[1] in ("-e", "--eval"):
        return Decision("ask", "ask: interpreter 'node -e'")
    return None


def _allow_decision(rule: AllowRule, basename: str, command: tuple[str, ...]) -> Decision | None:
    """Return the decision for one allow rule.

    ``None`` means the rule does not apply. When the command names the rule but
    uses a subcommand or flag the rule forbids, the returned decision is
    ``ask``, not ``allow``; extra_allow must not widen a rule's own
    restrictions.
    """
    if rule.cmd != basename:
        return None
    if rule.subcommands:
        if len(command) < 2 or command[1] not in rule.subcommands:
            return Decision(
                "ask",
                f"ask: rule {rule.cmd} allows subcommands "
                f"{', '.join(repr(item) for item in rule.subcommands)} only",
            )
        args_to_check = command[2:]
        subcommand = command[1]
    else:
        args_to_check = command[1:]
        subcommand = None
    for argument in args_to_check:
        flag = _denied_flag(argument, rule.deny_flags)
        if flag is not None:
            return Decision("ask", f"ask: rule {rule.cmd} denies flag {flag!r}")
    if subcommand is None:
        return Decision("allow", f"allow: rule {rule.cmd}")
    return Decision("allow", f"allow: rule {rule.cmd} {subcommand}")


def _denied_flag(argument: str, deny_flags: Sequence[str]) -> str | None:
    """Return the deny flag ``argument`` spells, if any.

    ``--pre=sh`` is the same flag as ``--pre sh``, so a long flag also matches
    its ``--flag=value`` form. Exact match only otherwise: ``-exec`` must not
    match an unrelated ``-executable``.
    """
    for flag in deny_flags:
        if argument == flag or (flag.startswith("--") and argument.startswith(f"{flag}=")):
            return flag
    return None


def _extra_allow_decision(
    command: tuple[str, ...], extra_allow: Sequence[Sequence[str]]
) -> Decision | None:
    """Match owner-registered test commands element by element.

    Each entry is unwrapped exactly like the command (``["uv", "run",
    "pytest"]`` becomes ``("pytest",)``), so a repo that registered its test
    command through ``uv run`` matches both ``uv run pytest -k x`` and
    ``pytest -k x``.
    """
    for entry in extra_allow:
        if isinstance(entry, str) or not entry:
            continue
        if not all(isinstance(part, str) for part in entry):
            continue
        unwrapped = _unwrap_all(tuple(entry))
        if isinstance(unwrapped, Decision):
            continue
        if len(unwrapped) < 2 and _is_tripwire_program(unwrapped[0]):
            # A bare ``bash``, ``python`` or ``npm`` entry would allow every
            # invocation of it. Only a specific command line can be registered.
            continue
        if len(unwrapped) <= len(command) and command[: len(unwrapped)] == unwrapped:
            return Decision("allow", f"allow: extra_allow {' '.join(entry)}")
    return None


def _is_tripwire_program(program: str) -> bool:
    """True for programs that run arbitrary code or installs from their arguments."""
    basename = _basename(program)
    return (
        basename in _SHELL_WRAPPERS
        or basename in _INTERPRETERS
        or basename in _PACKAGE_MANAGERS
        or basename in ("python", "python3", "node")
    )


def _unwrap_all(command: tuple[str, ...]) -> tuple[str, ...] | Decision:
    """Peel known safe wrappers off ``command`` until a real program is left.

    Returns the inner command, or an ``ask`` decision for a wrapper used in a
    form this policy does not understand, or nested too deeply.
    """
    for _ in range(MAX_UNWRAP_DEPTH):
        decision, inner = _unwrap_once(command)
        if decision is not None:
            return decision
        if inner == command:
            return command
        command = inner
    return Decision("ask", f"ask: more than {MAX_UNWRAP_DEPTH} nested wrappers")


def _unwrap_once(command: tuple[str, ...]) -> tuple[Decision | None, tuple[str, ...]]:
    """Unwrap one known safe wrapper, or return an ``ask`` decision."""
    program = command[0]
    if program == "uv":
        return _unwrap_uv(command)
    if program in ("python", "python3") and len(command) > 1 and command[1] == "-m":
        if len(command) < 3:
            return Decision("ask", "ask: python -m requires a module name"), command
        return None, (command[2], *command[3:])
    if program == "npx":
        return _unwrap_npx(command)
    return None, command


def _unwrap_uv(command: tuple[str, ...]) -> tuple[Decision | None, tuple[str, ...]]:
    """Unwrap ``uv run [--frozen|--offline|--no-sync|--quiet] <cmd>``."""
    if len(command) < 2 or command[1] != "run":
        return (
            Decision("ask", "ask: uv is only allowed as 'uv run [flags] <command>'"),
            command,
        )
    index = 2
    while index < len(command) and command[index] in _UV_RUN_FLAGS:
        index += 1
    if index == len(command):
        return Decision("ask", "ask: uv run has no command"), command
    if command[index].startswith("-"):
        return (
            Decision(
                "ask",
                f"ask: unsupported uv run flag {command[index]!r}; "
                "only --frozen, --offline, --no-sync and --quiet may precede the command",
            ),
            command,
        )
    return None, command[index:]


def _unwrap_npx(command: tuple[str, ...]) -> tuple[Decision | None, tuple[str, ...]]:
    """Unwrap ``npx --no-install <cmd>``."""
    if len(command) < 3 or command[1] != "--no-install":
        return (
            Decision("ask", "ask: npx is only allowed as 'npx --no-install <command>'"),
            command,
        )
    return None, command[2:]
