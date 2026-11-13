"""Smoke tests for the package skeleton created by doc 01."""

import importlib

import pytest

import gaia

SUBPACKAGES = [
    "api",
    "config",
    "db",
    "domain",
    "llm",
    "orchestrator",
    "runtimes",
    "security",
    "services",
    "tools",
]


def test_version_is_exposed() -> None:
    assert gaia.__version__ == "0.1.0"


@pytest.mark.parametrize("name", SUBPACKAGES)
def test_subpackage_imports(name: str) -> None:
    module = importlib.import_module(f"gaia.{name}")
    assert module.__doc__, f"gaia.{name} needs a docstring that says what it owns"


async def test_async_tests_run() -> None:
    # Guards the pytest-asyncio configuration (asyncio_mode = "auto").
    assert True
