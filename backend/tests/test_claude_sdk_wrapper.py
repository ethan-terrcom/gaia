"""Tests for the 10c cli_path wrapper script."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from gaia.runtimes.claude_agent_sdk import build_wrapper_script
from gaia.services.proc import run_proc


def _write_executable(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    path.chmod(0o700)
    return path


def _stub_srt(path: Path) -> Path:
    return _write_executable(
        path,
        f"""#!{sys.executable}
import os
import sys

args = sys.argv[1:]
index = args.index("--")
command = args[index + 1 :]
os.execv(command[0], command)
""",
    )


def _stub_cli(path: Path) -> Path:
    return _write_executable(
        path,
        f"""#!{sys.executable}
import json
import os
import sys

print(json.dumps({{"argv": sys.argv[1:], "env": dict(os.environ)}}))
""",
    )


async def test_wrapper_runs_under_sh_and_scrubs_the_environment(tmp_path: Path) -> None:
    task_dir = tmp_path / "task"
    task_dir.mkdir()
    srt_path = _stub_srt(tmp_path / "srt")
    cli_path = _stub_cli(tmp_path / "claude")
    settings_path = task_dir / "srt-settings.json"
    settings_path.write_text("{}", encoding="utf-8")
    pid_file = task_dir / "claude.pid"
    agent_path = "/usr/local/bin:/usr/bin:/bin"

    wrapper_text = build_wrapper_script(
        srt_path=srt_path,
        settings_path=settings_path,
        cli_binary=cli_path,
        pid_file=pid_file,
        agent_path=agent_path,
        task_dir=task_dir,
        approval_timeout_s=600,
    )
    wrapper_path = _write_executable(task_dir / "claude-wrapper", wrapper_text)

    result = await run_proc(
        [str(wrapper_path)],
        cwd=tmp_path,
        env={
            "GAIA_RUN_TOKEN": "gaia_rt_planted-token",
            "GAIA_ANTHROPIC_BASE_URL": "http://127.0.0.1:8765/llm/anthropic",
            "CLAUDE_CODE_ENTRYPOINT": "sdk-py",
            "CLAUDE_AGENT_SDK_VERSION": "0.2.175",
            "ANTHROPIC_API_KEY_REAL": "sk-ant-planted-real",
            "DEEPSEEK_API_KEY": "sk-planted-deepseek",
            "NO_PROXY": "localhost,127.0.0.1",
            "no_proxy": "localhost",
        },
    )

    assert result.ok, result.stderr
    payload = json.loads(result.stdout)
    environment = payload["env"]
    assert environment["ANTHROPIC_API_KEY"] == "gaia_rt_planted-token"
    assert environment["ANTHROPIC_BASE_URL"] == "http://127.0.0.1:8765/llm/anthropic"
    assert "ANTHROPIC_API_KEY_REAL" not in environment
    assert "DEEPSEEK_API_KEY" not in environment
    assert "NO_PROXY" not in environment
    assert "no_proxy" not in environment
    assert environment["PATH"] == agent_path
    assert environment["HOME"] == str(task_dir)
    assert environment["TMPDIR"] == str(task_dir)
    assert environment["LANG"] == "C.UTF-8"
    assert environment["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] == "1"
    assert environment["DISABLE_AUTOUPDATER"] == "1"
    # Lead review C3: approval wait + the longest run_command + padding.
    assert environment["MCP_TOOL_TIMEOUT"] == str((600 + 1800 + 120) * 1000)
    assert environment["CLAUDE_CODE_ENTRYPOINT"] == "sdk-py"
    assert environment["CLAUDE_AGENT_SDK_VERSION"] == "0.2.175"

    assert pid_file.exists()
    assert int(pid_file.read_text(encoding="ascii")) > 0


async def test_wrapper_quoting_holds_for_a_path_with_space_and_quote(tmp_path: Path) -> None:
    task_dir = tmp_path / "task ' quoted"
    task_dir.mkdir()
    srt_path = _stub_srt(tmp_path / "srt ' quoted")
    cli_path = _stub_cli(tmp_path / "claude ' quoted")
    settings_path = task_dir / "settings ' quoted.json"
    settings_path.write_text("{}", encoding="utf-8")
    pid_file = task_dir / "pid ' quoted"
    agent_path = "/bin"

    wrapper_text = build_wrapper_script(
        srt_path=srt_path,
        settings_path=settings_path,
        cli_binary=cli_path,
        pid_file=pid_file,
        agent_path=agent_path,
        task_dir=task_dir,
        approval_timeout_s=0,
    )
    wrapper_path = _write_executable(task_dir / "claude-wrapper", wrapper_text)

    result = await run_proc(
        [str(wrapper_path), "hello 'quoted'"],
        cwd=tmp_path,
        env={
            "GAIA_RUN_TOKEN": "gaia_rt_quote-test",
            "GAIA_ANTHROPIC_BASE_URL": "http://127.0.0.1:1/llm/anthropic",
            "CLAUDE_CODE_ENTRYPOINT": "sdk-py",
            "CLAUDE_AGENT_SDK_VERSION": "0.2.175",
        },
    )

    assert result.ok, result.stderr
    payload = json.loads(result.stdout)
    assert payload["argv"] == ["hello 'quoted'"]
    assert payload["env"]["HOME"] == str(task_dir)
    assert pid_file.read_text(encoding="ascii").strip().isdigit()


def test_wrapper_script_refuses_newline_or_nul_paths(tmp_path: Path) -> None:
    task_dir = tmp_path / "task"
    task_dir.mkdir()
    srt_path = tmp_path / "srt"
    settings_path = tmp_path / "settings"
    cli_path = tmp_path / "cli"
    pid_file = tmp_path / "pid"

    with pytest.raises(ValueError, match="newline or NUL"):
        build_wrapper_script(
            srt_path=Path("bad\npath"),
            settings_path=settings_path,
            cli_binary=cli_path,
            pid_file=pid_file,
            agent_path="/bin",
            task_dir=task_dir,
            approval_timeout_s=0,
        )
    with pytest.raises(ValueError, match="newline or NUL"):
        build_wrapper_script(
            srt_path=srt_path,
            settings_path=Path("bad\0path"),
            cli_binary=cli_path,
            pid_file=pid_file,
            agent_path="/bin",
            task_dir=task_dir,
            approval_timeout_s=0,
        )
