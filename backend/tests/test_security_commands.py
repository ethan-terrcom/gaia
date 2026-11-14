"""Doc 09a task 2: command policy rule table."""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest
from pydantic import ValidationError

from gaia.security.commands import CommandPolicy
from gaia.security.decision import Decision


@pytest.fixture
def policy() -> CommandPolicy:
    return CommandPolicy.from_default()


def evaluate(policy: CommandPolicy, argv: list[object], **kwargs: object) -> Decision:
    """Small helper so the table below reads as argv → decision."""
    return policy.evaluate(argv, **kwargs)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("argv", "reason"),
    [
        ([], "argv is empty"),
        (["echo", 3], "every argument must be a str"),
        (["echo", "a\x00b"], "NUL byte in argument"),
        (["echo\n", "x"], "newline in program name"),
        (["echo"] * 257, "at most 256 arguments"),
        (["echo", "a" * 4097], "at most 4096 characters"),
    ],
)
def test_evaluate_denies_malformed_argv(
    policy: CommandPolicy, argv: list[object], reason: str
) -> None:
    decision = evaluate(policy, argv)

    assert decision.action == "deny"
    assert reason in decision.reason


def test_evaluate_denies_a_string_argv(policy: CommandPolicy) -> None:
    decision = policy.evaluate("echo hello")

    assert decision.action == "deny"
    assert "expected a list of arguments" in decision.reason


@pytest.mark.parametrize(
    ("argv", "reason"),
    [
        (["git", "push"], "use the git tools (09b)"),
        (["/usr/bin/git", "status"], "use the git tools (09b)"),
        (["sudo", "id"], "privilege escalation tool 'sudo'"),
        (["su", "root"], "privilege escalation tool 'su'"),
        (["doas", "id"], "privilege escalation tool 'doas'"),
        (["curl", "https://example.com"], "network tool 'curl'"),
        (["wget", "https://example.com"], "network tool 'wget'"),
        (["ssh", "host"], "network tool 'ssh'"),
        (["scp", "a", "b"], "network tool 'scp'"),
        (["sftp", "host"], "network tool 'sftp'"),
        (["rsync", "a", "b"], "network tool 'rsync'"),
        (["nc", "-l", "1234"], "network tool 'nc'"),
        (["ncat", "host"], "network tool 'ncat'"),
        (["netcat", "host"], "network tool 'netcat'"),
        (["telnet", "host"], "network tool 'telnet'"),
        (["ftp", "host"], "network tool 'ftp'"),
        (["socat", "-", "-"], "network tool 'socat'"),
        (["rm", "-rf", "/"], "destructive tool 'rm'"),
        (["rmdir", "x"], "destructive tool 'rmdir'"),
        (["dd", "if=/dev/zero", "of=/dev/disk1"], "destructive tool 'dd'"),
        (["mkfs", "/dev/disk1"], "destructive tool 'mkfs'"),
        (["shred", "x"], "destructive tool 'shred'"),
        (["diskutil", "eraseDisk"], "destructive tool 'diskutil'"),
        (["open", "/Applications"], "macOS escape tool 'open'"),
        (["osascript", "-e", "1"], "macOS escape tool 'osascript'"),
        (["security", "dump-keychain"], "macOS escape tool 'security'"),
        (["launchctl", "load"], "macOS escape tool 'launchctl'"),
        (["defaults", "write"], "macOS escape tool 'defaults'"),
        (["pbcopy"], "macOS escape tool 'pbcopy'"),
        (["pbpaste"], "macOS escape tool 'pbpaste'"),
        (["screencapture", "x"], "macOS escape tool 'screencapture'"),
        (["crontab", "-e"], "persistence tool 'crontab'"),
        (["at", "now"], "persistence tool 'at'"),
        (["chown", "root", "x"], "ownership tool 'chown'"),
        (["chmod", "u+s", "x"], "chmod with setuid/setgid flags"),
        (["chmod", "+s", "x"], "chmod with setuid/setgid flags"),
        (["kill", "1"], "process control tool 'kill'"),
        (["pkill", "x"], "process control tool 'pkill'"),
        (["killall", "x"], "process control tool 'killall'"),
    ],
)
def test_evaluate_denies_dangerous_commands(
    policy: CommandPolicy, argv: list[object], reason: str
) -> None:
    decision = evaluate(policy, argv)

    assert decision.action == "deny"
    assert reason in decision.reason


@pytest.mark.parametrize(
    ("argv", "reason"),
    [
        (["./pytest"], "command path './pytest' is not allowlisted"),
        (["/bin/ls"], "command path '/bin/ls' is not allowlisted"),
        (["bash", "-c", "echo hi"], "shell or process wrapper 'bash'"),
        (["sh", "-c", "echo hi"], "shell or process wrapper 'sh'"),
        (["zsh"], "shell or process wrapper 'zsh'"),
        (["fish"], "shell or process wrapper 'fish'"),
        (["dash"], "shell or process wrapper 'dash'"),
        (["ksh"], "shell or process wrapper 'ksh'"),
        (["env", "X=1"], "shell or process wrapper 'env'"),
        (["xargs"], "shell or process wrapper 'xargs'"),
        (["exec", "ls"], "shell or process wrapper 'exec'"),
        (["eval", "ls"], "shell or process wrapper 'eval'"),
        (["nohup", "ls"], "shell or process wrapper 'nohup'"),
        (["timeout", "1", "ls"], "shell or process wrapper 'timeout'"),
        (["nice", "ls"], "shell or process wrapper 'nice'"),
        (["time", "ls"], "shell or process wrapper 'time'"),
        (["python", "script.py"], "interpreter 'python'"),
        (["python", "-c", "print(1)"], "interpreter 'python'"),
        (["node", "-e", "1"], "interpreter 'node -e'"),
        (["perl", "-e", "1"], "interpreter 'perl'"),
        (["ruby", "-e", "1"], "interpreter 'ruby'"),
        (["php", "-r", "1"], "interpreter 'php'"),
        (["deno", "run"], "interpreter 'deno'"),
        (["bun", "run"], "interpreter 'bun'"),
        (["uv", "run", "--with", "x", "pytest"], "unsupported uv run flag"),
        (["uv", "run"], "uv run has no command"),
        (["uv", "tool", "install"], "uv is only allowed"),
        (["npm", "install"], "package manager 'npm' requires approval"),
        (["pip", "install", "x"], "package manager 'pip' requires approval"),
        (["npx", "--yes", "eslint"], "npx is only allowed"),
        (["python", "-m"], "python -m requires a module name"),
        (["cargo", "run"], "allows subcommands"),
        (["go", "build"], "allows subcommands"),
        (["find", ".", "-delete"], "denies flag '-delete'"),
        (["find", ".", "-exec", "rm"], "denies flag '-exec'"),
        (["find", ".", "-execdir", "rm"], "denies flag '-execdir'"),
        (["find", ".", "-ok", "rm"], "denies flag '-ok'"),
    ],
)
def test_evaluate_asks_for_paths_shells_interpreters_and_restricted_rules(
    policy: CommandPolicy, argv: list[object], reason: str
) -> None:
    decision = evaluate(policy, argv)

    assert decision.action == "ask"
    assert reason in decision.reason


@pytest.mark.parametrize(
    ("argv", "reason"),
    [
        (["pytest", "-k", "x"], "allow: rule pytest"),
        (["ruff", "check", "."], "allow: rule ruff"),
        (["mypy"], "allow: rule mypy"),
        (["black", "."], "allow: rule black"),
        (["isort", "."], "allow: rule isort"),
        (["eslint", "src"], "allow: rule eslint"),
        (["prettier", "--check", "."], "allow: rule prettier"),
        (["tsc", "--noEmit"], "allow: rule tsc"),
        (["vitest", "run"], "allow: rule vitest"),
        (["jest"], "allow: rule jest"),
        (["cargo", "test"], "allow: rule cargo test"),
        (["cargo", "check"], "allow: rule cargo check"),
        (["cargo", "build"], "allow: rule cargo build"),
        (["cargo", "fmt"], "allow: rule cargo fmt"),
        (["cargo", "clippy"], "allow: rule cargo clippy"),
        (["go", "test"], "allow: rule go test"),
        (["ls"], "allow: rule ls"),
        (["cat", "README.md"], "allow: rule cat"),
        (["head", "-n", "1", "x"], "allow: rule head"),
        (["tail", "x"], "allow: rule tail"),
        (["wc", "-l", "x"], "allow: rule wc"),
        (["grep", "-n", "x"], "allow: rule grep"),
        (["rg", "pattern"], "allow: rule rg"),
        (["find", ".", "-name", "x"], "allow: rule find"),
        (["diff", "a", "b"], "allow: rule diff"),
        (["echo", "hello"], "allow: rule echo"),
        (["pwd"], "allow: rule pwd"),
        (["true"], "allow: rule true"),
        (["false"], "allow: rule false"),
    ],
)
def test_evaluate_allows_default_rules(
    policy: CommandPolicy, argv: list[object], reason: str
) -> None:
    decision = evaluate(policy, argv)

    assert decision.action == "allow"
    assert decision.reason == reason


@pytest.mark.parametrize(
    ("argv", "reason"),
    [
        (["uv", "run", "pytest", "-k", "x"], "allow: rule pytest"),
        (["uv", "run", "--frozen", "pytest"], "allow: rule pytest"),
        (["uv", "run", "--offline", "--no-sync", "pytest"], "allow: rule pytest"),
        (["uv", "run", "--quiet", "pytest"], "allow: rule pytest"),
        (["uv", "run", "-q", "pytest"], "allow: rule pytest"),
        (["python", "-m", "pytest"], "allow: rule pytest"),
        (["python3", "-m", "pytest"], "allow: rule pytest"),
        (["npx", "--no-install", "eslint", "src"], "allow: rule eslint"),
    ],
)
def test_evaluate_unwraps_known_wrappers(
    policy: CommandPolicy, argv: list[object], reason: str
) -> None:
    decision = evaluate(policy, argv)

    assert decision.action == "allow"
    assert decision.reason == reason


def test_extra_allow_upgrades_an_unknown_command(policy: CommandPolicy) -> None:
    decision = policy.evaluate(["custom-test", "--ci"], extra_allow=(("custom-test",),))

    assert decision.action == "allow"
    assert decision.reason == "allow: extra_allow custom-test"


def test_extra_allow_matches_element_by_element(policy: CommandPolicy) -> None:
    decision = policy.evaluate(
        ["custom-test", "--ci", "extra"],
        extra_allow=(("custom-test", "--ci"),),
    )

    assert decision.action == "allow"
    assert decision.reason == "allow: extra_allow custom-test --ci"


def test_extra_allow_does_not_match_a_shorter_prefix(policy: CommandPolicy) -> None:
    decision = policy.evaluate(["custom-test"], extra_allow=(("custom-test", "--ci"),))

    assert decision.action == "ask"


def test_extra_allow_never_overrides_a_deny(policy: CommandPolicy) -> None:
    decision = policy.evaluate(["curl", "https://example.com"], extra_allow=(("curl",),))

    assert decision.action == "deny"
    assert "network tool 'curl'" in decision.reason


def test_extra_allow_permits_an_owner_registered_shell_script(policy: CommandPolicy) -> None:
    """Lead change (review 09a, S2): the owner's registered test command wins over tripwires."""
    decision = policy.evaluate(
        ["bash", "scripts/test.sh", "--fast"], extra_allow=(("bash", "scripts/test.sh"),)
    )

    assert decision.action == "allow"
    assert decision.reason == "allow: extra_allow bash scripts/test.sh"


@pytest.mark.parametrize("entry", [("bash",), ("python",), ("npm",), ("uv", "run", "python")])
def test_extra_allow_ignores_a_bare_tripwire_program(
    policy: CommandPolicy, entry: tuple[str, ...]
) -> None:
    argv = ["python", "-c", "x"] if "python" in entry else [entry[0], "-c", "x"]

    decision = policy.evaluate(argv, extra_allow=(entry,))

    assert decision.action == "ask"


@pytest.mark.parametrize(
    ("argv", "entry"),
    [
        (["npm", "test"], ("npm", "test")),
        (["uv", "run", "pytest", "-k", "x"], ("uv", "run", "pytest")),
        (["pytest", "-k", "x"], ("uv", "run", "pytest")),
        (["uv", "run", "python", "-m", "pytest"], ("python", "-m", "pytest")),
    ],
)
def test_extra_allow_entries_are_unwrapped_like_the_command(
    policy: CommandPolicy, argv: list[str], entry: tuple[str, ...]
) -> None:
    decision = policy.evaluate(argv, extra_allow=(entry,))

    assert decision.action == "allow"


def test_extra_allow_skips_an_entry_with_an_unsupported_wrapper(policy: CommandPolicy) -> None:
    decision = policy.evaluate(["custom"], extra_allow=(("uv", "pip", "install", "x"),))

    assert decision.action == "ask"


@pytest.mark.parametrize(
    ("argv", "action", "reason"),
    [
        (["uv", "run", "python", "-m", "pytest", "-q"], "allow", "allow: rule pytest"),
        (["uv", "run", "uv", "run", "uv", "run", "uv", "run", "pytest"], "ask", "nested wrappers"),
        (["rg", "--pre", "sh", "x"], "ask", "denies flag '--pre'"),
        (["rg", "--pre=sh", "x"], "ask", "denies flag '--pre'"),
        (["find", ".", "-okdir", "rm", ";"], "ask", "denies flag '-okdir'"),
        (["find", ".", "-fprint", "out"], "ask", "denies flag '-fprint'"),
        (["find", ".", "-executable"], "allow", "allow: rule find"),
        (["chmod", "4755", "x"], "deny", "setuid"),
        (["chmod", "02755", "x"], "deny", "setuid"),
        (["chmod", "g+sx", "x"], "deny", "setuid"),
        (["chmod", "755", "x"], "ask", "no rule matched"),
        (["sudoedit", "/etc/hosts"], "deny", "privilege escalation"),
        (["pkexec", "id"], "deny", "privilege escalation"),
    ],
)
def test_lead_review_cases(
    policy: CommandPolicy, argv: list[str], action: str, reason: str
) -> None:
    """Cases added in the lead review of 09a (S2-S4)."""
    decision = policy.evaluate(argv)

    assert decision.action == action
    assert reason in decision.reason


def test_extra_allow_never_overrides_a_slashed_program(policy: CommandPolicy) -> None:
    decision = policy.evaluate(["./pytest"], extra_allow=(("./pytest",),))

    assert decision.action == "ask"
    assert "command path './pytest' is not allowlisted" in decision.reason


def test_extra_allow_skips_a_string_entry(policy: CommandPolicy) -> None:
    decision = policy.evaluate(["custom-test"], extra_allow=("custom-test",))

    assert decision.action == "ask"


def test_extra_allow_skips_entries_with_non_string_parts(policy: CommandPolicy) -> None:
    decision = policy.evaluate(
        ["custom-test"],
        extra_allow=([], [1]),  # type: ignore[list-item]
    )

    assert decision.action == "ask"


def test_from_toml_loads_a_custom_policy(tmp_path: Path) -> None:
    path = tmp_path / "policy.toml"
    path.write_text('[[allow]]\ncmd = "custom-test"\n', encoding="utf-8")

    policy = CommandPolicy.from_toml(path)

    decision = policy.evaluate(["custom-test"])
    assert decision.action == "allow"
    assert decision.reason == "allow: rule custom-test"


def test_from_toml_rejects_unknown_keys(tmp_path: Path) -> None:
    path = tmp_path / "policy.toml"
    path.write_text('[[allow]]\ncmd = "pytest"\nunknown = true\n', encoding="utf-8")

    with pytest.raises(ValidationError):
        CommandPolicy.from_toml(path)


def test_from_toml_rejects_malformed_toml(tmp_path: Path) -> None:
    path = tmp_path / "policy.toml"
    path.write_text("[[allow\ncmd = 'pytest'\n", encoding="utf-8")

    with pytest.raises(tomllib.TOMLDecodeError):
        CommandPolicy.from_toml(path)


def test_default_policy_exists_and_is_loadable() -> None:
    policy = CommandPolicy.from_default()

    assert policy.allow_rules
