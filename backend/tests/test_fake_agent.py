"""Direct tests for the stdlib-only fake-agent script.

The subprocess contract exercises this file by running it under ``stream_proc``,
but coverage cannot follow a child process. These tests import it in-process and
drive each action through the same functions.
"""

from __future__ import annotations

import json
import runpy
import sys
import time
import uuid
from pathlib import Path

import pytest

import gaia.runtimes.fake_agent as fake_agent


def _output(capsys: pytest.CaptureFixture[str]) -> list[dict[str, object]]:
    captured = capsys.readouterr()
    return [json.loads(line) for line in captured.out.splitlines() if line]


def test_emit_writes_one_json_line(capsys: pytest.CaptureFixture[str]) -> None:
    fake_agent._emit({"type": "message", "payload": {"content": "hi"}})

    assert _output(capsys) == [{"type": "message", "payload": {"content": "hi"}}]


def test_decision_dir_uses_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))

    assert fake_agent._decision_dir() == tmp_path / "approvals"


def test_decision_dir_falls_back_to_cwd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HOME")
    monkeypatch.chdir(tmp_path)

    assert fake_agent._decision_dir() == tmp_path / "approvals"


def test_run_message_write_env_sleep_and_finish(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    target = tmp_path / "hello.txt"

    exit_code = fake_agent._run(
        [
            {"type": "message", "payload": {"content": "hi"}},
            {"type": "write", "path": str(target), "content": "hello"},
            {"type": "env"},
            {"type": "sleep", "seconds": 0},
            {"type": "finish", "summary": "done", "structured_output": {"ok": True}},
        ],
        tmp_path,
    )

    assert exit_code == 0
    assert target.read_text() == "hello"
    lines = _output(capsys)
    assert lines[0]["type"] == "message"
    assert lines[1]["type"] == "write"
    assert lines[2]["type"] == "env"
    assert lines[3]["type"] == "finish"


def test_run_emit_and_approval(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(uuid, "uuid4", lambda: uuid.UUID("00000000-0000-0000-0000-000000000000"))
    decision_dir = tmp_path / "approvals"
    decision_dir.mkdir()
    (decision_dir / "00000000000000000000000000000000.json").write_text(
        json.dumps({"approved": True, "reason": "ok"})
    )

    exit_code = fake_agent._run(
        [
            {"type": "emit", "event_type": "agent.tool_call", "payload": {"name": "x"}},
            {"type": "approval", "argv": ["pytest"]},
        ],
        decision_dir,
    )

    assert exit_code == 0
    lines = _output(capsys)
    assert lines[0]["type"] == "emit"
    assert lines[1]["type"] == "approval"


def test_run_fail_prints_stderr_and_returns_nonzero(
    capsys: pytest.CaptureFixture[str],
) -> None:
    exit_code = fake_agent._run([{"type": "fail", "message": "boom"}], Path("/tmp"))

    assert exit_code == 4
    assert "boom" in capsys.readouterr().err


def test_run_write_requires_a_string_path() -> None:
    assert fake_agent._run([{"type": "write", "path": None}], Path("/tmp")) == 3


def test_run_ignores_unknown_action_types() -> None:
    assert fake_agent._run([{"type": "unknown"}], Path("/tmp")) == 0


def test_wait_for_decision_reads_an_existing_file(tmp_path: Path) -> None:
    decision = {"approved": False, "reason": "no"}
    (tmp_path / "abc.json").write_text(json.dumps(decision))

    assert fake_agent._wait_for_decision(tmp_path, "abc") == decision


def test_wait_for_decision_polls_until_the_file_appears(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    decision = {"approved": True, "reason": "yes"}
    decision_path = tmp_path / "abc.json"

    def _write_then_return(seconds: float) -> None:
        del seconds
        decision_path.write_text(json.dumps(decision))

    monkeypatch.setattr(time, "sleep", _write_then_return)

    assert fake_agent._wait_for_decision(tmp_path, "abc") == decision


def test_main_requires_a_script_path(capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(sys, "argv", ["fake_agent.py"])
    try:
        assert fake_agent.main() == 2
    finally:
        monkeypatch.undo()
    assert "usage" in capsys.readouterr().err


def test_main_runs_a_script(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    script = tmp_path / "script.json"
    script.write_text(json.dumps([{"type": "message", "payload": {"content": "hi"}}]))
    monkeypatch.setattr(sys, "argv", ["fake_agent.py", str(script)])
    monkeypatch.setenv("HOME", str(tmp_path))

    assert fake_agent.main() == 0
    assert _output(capsys)[0]["type"] == "message"


def test_fake_agent_can_be_run_as_a_script(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys, "argv", ["fake_agent.py"])

    with pytest.raises(SystemExit) as excinfo:
        runpy.run_path(str(Path(fake_agent.__file__).resolve()), run_name="__main__")

    assert excinfo.value.code == 2
