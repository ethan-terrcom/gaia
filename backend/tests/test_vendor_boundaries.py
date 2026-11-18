"""P1: vendor SDK imports stay inside their single adapter modules.

An AST walk over every Python file in ``gaia/`` finds imports even when they
are hidden inside functions or conditional branches. The rule is deliberately
mechanical: it must fail before a reviewer has to notice that a second module
started importing ``openai``.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

GAIA_DIR = Path(__file__).resolve().parents[1] / "gaia"

ALLOWED: dict[str, frozenset[str]] = {
    "anthropic": frozenset({"llm/anthropic_client.py"}),
    "openai": frozenset({"llm/openai_compat_client.py"}),
    "claude_agent_sdk": frozenset({"runtimes/claude_agent_sdk.py"}),
}

#: The boundary test must not pass vacuously because it walked the wrong root.
MIN_FILES = 50


def _imported_roots(tree: ast.AST) -> list[str]:
    """Return top-level module names imported anywhere in ``tree``."""
    roots: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.extend(alias.name.split(".", 1)[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            module = node.module
            if module is not None:
                roots.append(module.split(".", 1)[0])
    return roots


def _python_files() -> tuple[Path, ...]:
    return tuple(sorted(GAIA_DIR.rglob("*.py")))


def test_vendor_imports_are_confined_to_their_adapters() -> None:
    files = _python_files()
    assert len(files) >= MIN_FILES, f"vendor boundary walk found only {len(files)} files"

    for path in files:
        relative = path.relative_to(GAIA_DIR).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for root in _imported_roots(tree):
            allowed = ALLOWED.get(root)
            if allowed is None:
                continue
            if relative not in allowed:
                pytest.fail(
                    f"{relative} imports {root!r}; {root} may only be imported in "
                    f"{sorted(allowed)} (ADR-001 P1)"
                )
