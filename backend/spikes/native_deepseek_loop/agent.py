"""A minimal, confined DeepSeek coding agent (lead-run harness, not part of Gaia).

- File tools are confined to one git worktree; `.git` cannot be read or written.
- Commands are allowlisted argv lists, run without a shell under macOS sandbox-exec:
  no network, writes only to the worktree + a per-run tmp + uv's cache, and no reads of
  shell profiles, ~/.ssh, ~/.aws, ~/.config, ~/.gaia, ~/.claude or the keychains.
- The API key is read from DEEPSEEK_KEY, removed from os.environ, and never passed to a child.
- Every request/response and tool call is appended to a JSONL transcript.

usage: agent.py --worktree DIR --task FILE --log FILE [--model M] [--max-turns N]
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import httpx

API = "https://api.deepseek.com/chat/completions"
HOME = str(Path.home())
TOOL_OUTPUT_CAP = 24_000
SKIP_DIRS = {
    ".git",
    ".venv",
    "node_modules",
    "__pycache__",
    ".mypy_cache",
    ".ruff_cache",
    ".pytest_cache",
    "dist",
    ".coverage",
}

ALLOWED_UV_RUN = {"pytest", "ruff", "mypy", "alembic", "python", "gaia"}
ALLOWED_GIT = {"status", "diff", "log", "show", "ls-files"}

SANDBOX_PROFILE = """(version 1)
(allow default)
(deny network* (remote ip))
(deny file-write*)
(allow file-write* (subpath "{wt}") (subpath "{tmp}") (subpath "{home}/.cache/uv")
                   (subpath "/dev") (subpath "/private/var/folders"))
(deny file-write* (literal "{wt}/.git") (subpath "{wt}/.git"))
(deny file-read* (literal "{home}/.zshrc") (literal "{home}/.zprofile") (literal "{home}/.zshenv")
                 (literal "{home}/.bash_profile") (literal "{home}/.bashrc") (literal "{home}/.zsh_history")
                 (subpath "{home}/.ssh") (subpath "{home}/.aws") (subpath "{home}/.config")
                 (subpath "{home}/.gnupg") (subpath "{home}/Library/Keychains") (subpath "{home}/.gaia")
                 (subpath "{home}/.claude") (subpath "{home}/.local/share/keyrings"))
"""

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read a text file in the worktree, with line numbers.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "offset": {"type": "integer", "description": "1-based first line"},
                    "limit": {"type": "integer"},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_dir",
            "description": "List files under a directory (depth <= 3).",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}, "depth": {"type": "integer"}},
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search",
            "description": "Regex search over text files (Python re). Returns path:line: text.",
            "parameters": {
                "type": "object",
                "properties": {"pattern": {"type": "string"}, "path": {"type": "string"}},
                "required": ["pattern"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Create or overwrite a file with the full content.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "edit_file",
            "description": "Replace an exact substring in a file. old must match exactly once unless replace_all.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "old": {"type": "string"},
                    "new": {"type": "string"},
                    "replace_all": {"type": "boolean"},
                },
                "required": ["path", "old", "new"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run",
            "description": (
                "Run a command (argv list, no shell) in a sandbox without network. Allowed: "
                "['uv','run',<pytest|ruff|mypy|alembic|python|gaia>, ...] and ['git',<status|diff|log|show|ls-files>, ...]. "
                "cwd is relative to the worktree root (default 'backend')."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "argv": {"type": "array", "items": {"type": "string"}},
                    "cwd": {"type": "string"},
                    "timeout_s": {"type": "integer"},
                },
                "required": ["argv"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "finish",
            "description": "Call once when done (or blocked) with your full report in markdown.",
            "parameters": {
                "type": "object",
                "properties": {"report": {"type": "string"}},
                "required": ["report"],
            },
        },
    },
]


class ToolError(Exception):
    pass


class Agent:
    def __init__(self, worktree: Path, log: Path, model: str, key: str) -> None:
        self.wt = worktree.resolve()
        self.log = log
        self.model = model
        self.key = key
        self.tmp = (log.parent / f"tmp-{log.stem}").resolve()
        self.tmp.mkdir(parents=True, exist_ok=True)
        self.profile = SANDBOX_PROFILE.format(wt=self.wt, tmp=self.tmp, home=HOME)
        self.usage = {"prompt_tokens": 0, "completion_tokens": 0, "prompt_cache_hit_tokens": 0}

    # -- helpers -------------------------------------------------------
    def _path(self, raw: str) -> Path:
        if "\x00" in raw:
            raise ToolError("NUL in path")
        p = (self.wt / raw).resolve() if not Path(raw).is_absolute() else Path(raw).resolve()
        if not p.is_relative_to(self.wt):
            raise ToolError(f"{raw!r} is outside the worktree")
        rel = p.relative_to(self.wt)
        if rel.parts and rel.parts[0] == ".git":
            raise ToolError(".git is off limits")
        return p

    def _record(self, kind: str, data: object) -> None:
        with self.log.open("a") as f:
            f.write(json.dumps({"t": time.time(), "kind": kind, "data": data}) + "\n")

    # -- tools ---------------------------------------------------------
    def read_file(self, path: str, offset: int = 1, limit: int = 400) -> str:
        lines = self._path(path).read_text(errors="replace").splitlines()
        start = max(offset, 1) - 1
        chunk = lines[start : start + max(1, min(limit, 1500))]
        body = "\n".join(f"{i + start + 1:5}\t{line}" for i, line in enumerate(chunk))
        return body + (
            f"\n... ({len(lines)} lines total)" if start + len(chunk) < len(lines) else ""
        )

    def list_dir(self, path: str = ".", depth: int = 2) -> str:
        root = self._path(path)
        out = []

        def walk(d: Path, level: int) -> None:
            for child in sorted(d.iterdir()):
                if child.name in SKIP_DIRS:
                    continue
                out.append(f"{'  ' * level}{child.name}{'/' if child.is_dir() else ''}")
                if child.is_dir() and level + 1 < min(depth, 3):
                    walk(child, level + 1)

        walk(root, 0)
        return "\n".join(out[:800])

    def search(self, pattern: str, path: str = ".") -> str:
        rx = re.compile(pattern)
        root = self._path(path)
        hits = []
        files = (
            [root]
            if root.is_file()
            else [
                p
                for p in root.rglob("*")
                if p.is_file() and not set(p.relative_to(self.wt).parts) & SKIP_DIRS
            ]
        )
        for f in files:
            if f.suffix in {".pyc", ".sqlite", ".png", ".lock"} or f.name == "package-lock.json":
                continue
            try:
                for n, line in enumerate(f.read_text(errors="ignore").splitlines(), 1):
                    if rx.search(line):
                        hits.append(f"{f.relative_to(self.wt)}:{n}: {line[:200]}")
                        if len(hits) >= 200:
                            return "\n".join(hits) + "\n... (truncated)"
            except OSError:
                continue
        return "\n".join(hits) or "(no matches)"

    def write_file(self, path: str, content: str) -> str:
        p = self._path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
        return f"wrote {p.relative_to(self.wt)} ({len(content)} chars)"

    def edit_file(self, path: str, old: str, new: str, replace_all: bool = False) -> str:
        p = self._path(path)
        text = p.read_text()
        count = text.count(old)
        if count == 0:
            raise ToolError("old string not found")
        if count > 1 and not replace_all:
            raise ToolError(f"old string matches {count} times; add context or set replace_all")
        p.write_text(text.replace(old, new) if replace_all else text.replace(old, new, 1))
        return f"edited {p.relative_to(self.wt)} ({count if replace_all else 1} replacement(s))"

    def run(self, argv: list[str], cwd: str = "backend", timeout_s: int = 600) -> str:
        if not argv or not all(isinstance(a, str) for a in argv):
            raise ToolError("argv must be a non-empty list of strings")
        if argv[0] == "uv":
            if len(argv) < 3 or argv[1] != "run" or argv[2] not in ALLOWED_UV_RUN:
                raise ToolError(f"only uv run {sorted(ALLOWED_UV_RUN)} are allowed")
        elif argv[0] == "git":
            if len(argv) < 2 or argv[1] not in ALLOWED_GIT:
                raise ToolError(f"only git {sorted(ALLOWED_GIT)} are allowed")
        else:
            raise ToolError("only uv run ... and read-only git are allowed")
        directory = self._path(cwd)
        env = {
            "PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin",
            "HOME": HOME,
            "LANG": "en_US.UTF-8",
            "TMPDIR": str(self.tmp),
            "UV_OFFLINE": "1",
            "UV_NO_SYNC": "1",
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GAIA_DATA_DIR": str(self.tmp / "gaia-data"),
        }
        try:
            proc = subprocess.run(
                ["sandbox-exec", "-p", self.profile, *argv],
                cwd=directory,
                env=env,
                capture_output=True,
                text=True,
                timeout=min(max(timeout_s, 1), 900),
                stdin=subprocess.DEVNULL,
                start_new_session=True,
            )
        except subprocess.TimeoutExpired:
            return f"TIMED OUT after {timeout_s}s"
        out = proc.stdout + ("\n[stderr]\n" + proc.stderr if proc.stderr.strip() else "")
        if len(out) > TOOL_OUTPUT_CAP:
            out = (
                out[: TOOL_OUTPUT_CAP // 3]
                + "\n... [middle truncated] ...\n"
                + out[-2 * TOOL_OUTPUT_CAP // 3 :]
            )
        return f"exit={proc.returncode}\n{out}"

    # -- loop ----------------------------------------------------------
    def call(self, messages: list[dict]) -> dict:
        for attempt in range(5):
            try:
                r = httpx.post(
                    API,
                    headers={"Authorization": f"Bearer {self.key}"},
                    timeout=600,
                    json={
                        "model": self.model,
                        "messages": messages,
                        "tools": TOOLS,
                        "tool_choice": "auto",
                        "max_tokens": 32000,
                    },
                )
            except httpx.HTTPError as e:
                self._record("http_error", str(e))
                time.sleep(5 * (attempt + 1))
                continue
            if r.status_code in (429, 500, 502, 503):
                time.sleep(10 * (attempt + 1))
                continue
            if r.status_code != 200:
                raise RuntimeError(f"API {r.status_code}: {r.text[:500]}")
            return r.json()
        raise RuntimeError("API kept failing")

    def loop(self, system: str, task: str, max_turns: int) -> str:
        messages: list[dict] = [
            {"role": "system", "content": system},
            {"role": "user", "content": task},
        ]
        for turn in range(max_turns):
            data = self.call(messages)
            for k in self.usage:
                self.usage[k] += int(data.get("usage", {}).get(k) or 0)
            msg = data["choices"][0]["message"]
            self._record("assistant", {"turn": turn, "msg": msg, "usage": data.get("usage")})
            keep = {"role": "assistant", "content": msg.get("content") or ""}
            if msg.get("reasoning_content"):
                keep["reasoning_content"] = msg["reasoning_content"]
            if msg.get("tool_calls"):
                keep["tool_calls"] = msg["tool_calls"]
            messages.append(keep)
            calls = msg.get("tool_calls") or []
            if not calls:
                messages.append(
                    {
                        "role": "user",
                        "content": "Continue. Use the tools; call finish with your report when done.",
                    }
                )
                continue
            for c in calls:
                name = c["function"]["name"]
                try:
                    args = json.loads(c["function"]["arguments"] or "{}")
                    if name == "finish":
                        self._record("finish", {"usage": self.usage})
                        return args.get("report", "")
                    result = (
                        getattr(self, name)(**args)
                        if name in {t["function"]["name"] for t in TOOLS}
                        else f"unknown tool {name}"
                    )
                except (ToolError, OSError, TypeError, ValueError, re.error) as e:
                    result = f"ERROR: {e}"
                result = (
                    result
                    if len(result) <= TOOL_OUTPUT_CAP
                    else result[:TOOL_OUTPUT_CAP] + "\n...[truncated]"
                )
                self._record(
                    "tool",
                    {
                        "name": name,
                        "args": c["function"]["arguments"][:2000],
                        "result": result[:4000],
                    },
                )
                messages.append({"role": "tool", "tool_call_id": c["id"], "content": result})
            print(
                f"turn {turn}: {[c['function']['name'] for c in calls]} tokens={self.usage}",
                flush=True,
            )
        return "MAX TURNS REACHED without finish()"


SYSTEM = """You are a junior software engineer on Gaia, working alone in a git worktree with tools.
Work carefully and completely: read the docs and code you need, implement, write tests, and run them.
Rules: never print or hard-code secrets; only edit files you are allowed to; never weaken lint/type/test
config; prefer small, reviewable changes; follow existing code style (docstrings, strict typing).
You cannot commit, push or install packages: the lead does that. When finished (all checks green) or
genuinely blocked, call finish with the report format given in your task."""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--worktree", type=Path, required=True)
    ap.add_argument("--task", type=Path, required=True)
    ap.add_argument("--log", type=Path, required=True)
    ap.add_argument("--report", type=Path, required=True)
    ap.add_argument("--model", default="deepseek-v4-pro")
    ap.add_argument("--max-turns", type=int, default=150)
    a = ap.parse_args()
    key = os.environ.pop("DEEPSEEK_KEY", "")
    for name in list(os.environ):
        if "KEY" in name or "TOKEN" in name or "SECRET" in name:
            os.environ.pop(name)
    if not key:
        print("DEEPSEEK_KEY not set", file=sys.stderr)
        return 2
    agent = Agent(a.worktree, a.log, a.model, key)
    report = agent.loop(SYSTEM, a.task.read_text(), a.max_turns)
    a.report.write_text(report + f"\n\n<!-- usage: {json.dumps(agent.usage)} -->\n")
    print("DONE", agent.usage)
    return 0


if __name__ == "__main__":
    sys.exit(main())
