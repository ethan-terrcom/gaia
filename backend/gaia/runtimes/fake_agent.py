"""A tiny stdlib-only fake agent for :class:`FakeProcessRuntime`.

Reads a JSON list of actions from the path in ``sys.argv[1]`` and performs them
in order. Output is one JSON object per line on stdout; the parent runtime maps
those lines to Gaia events. This program deliberately has no Gaia imports so it
can run in a scrubbed environment with only the standard library.
"""

from __future__ import annotations

import json
import os
import sys
import time
import uuid
from pathlib import Path
from typing import Any, cast


def _decision_dir() -> Path:
    """The directory where the parent runtime writes approval decisions."""
    home = os.environ.get("HOME")
    if home:
        path = Path(home) / "approvals"
        path.mkdir(parents=True, exist_ok=True)
        return path
    return Path.cwd() / "approvals"


def _emit(message: dict[str, Any]) -> None:
    """Print one JSON line for the parent runtime."""
    print(json.dumps(message, sort_keys=True))
    sys.stdout.flush()


def _wait_for_decision(decision_dir: Path, request_id: str) -> dict[str, Any]:
    """Poll for the parent's decision file until it exists."""
    decision_path = decision_dir / f"{request_id}.json"
    while not decision_path.exists():
        time.sleep(0.05)
    return cast("dict[str, Any]", json.loads(decision_path.read_text()))


def _run(script: list[dict[str, Any]], decision_dir: Path) -> int:
    """Execute one fake-agent script. Returns the process exit code."""
    for action in script:
        action_type = action.get("type")
        if action_type == "message":
            _emit({"type": "message", "payload": action.get("payload") or {}})
        elif action_type == "emit":
            _emit(
                {
                    "type": "emit",
                    "event_type": action.get("event_type"),
                    "payload": action.get("payload") or {},
                }
            )
        elif action_type == "write":
            path = action.get("path")
            content = action.get("content")
            if not isinstance(path, str):
                print("fake_agent: write action needs a string path", file=sys.stderr)
                return 3
            Path(path).write_text(str(content))
            _emit({"type": "write", "path": path})
        elif action_type == "env":
            _emit({"type": "env", "environment": dict(os.environ)})
        elif action_type == "sleep":
            time.sleep(float(action.get("seconds") or 0))
        elif action_type == "fail":
            print(str(action.get("message") or "fail"), file=sys.stderr)
            return 4
        elif action_type == "approval":
            request_id = uuid.uuid4().hex
            _emit({"type": "approval", "request_id": request_id, "argv": action.get("argv") or []})
            _wait_for_decision(decision_dir, request_id)
        elif action_type == "finish":
            _emit(
                {
                    "type": "finish",
                    "summary": action.get("summary") or "",
                    "structured_output": action.get("structured_output"),
                    "files_changed": action.get("files_changed") or [],
                }
            )
    return 0


def main() -> int:
    """Read the script path from argv and run it."""
    if len(sys.argv) != 2:
        print("usage: fake_agent.py <script.json>", file=sys.stderr)
        return 2
    script = json.loads(Path(sys.argv[1]).read_text())
    return _run(script, _decision_dir())


if __name__ == "__main__":
    raise SystemExit(main())
