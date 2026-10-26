"""DCA-008 — Python quality commands.

This story configures the project's quality gate (Ruff lint + `ruff format`, pytest, and one type
checker, mypy) with documented commands. It implements NO product behavior; these tests only assert
the tooling contract is declared so a fresh checkout can run the same commands the docs promise.
"""

import sys
import tomllib
from pathlib import Path

PYPROJECT = Path(__file__).resolve().parent.parent / "pyproject.toml"


def _config() -> dict:
    with PYPROJECT.open("rb") as fh:
        return tomllib.load(fh)


def test_minimal_passing_marker():
    # The story's required minimal passing test: no product behavior, just proves the suite runs.
    assert sys.version_info >= (3, 11)


def test_dev_dependencies_declare_the_quality_tools():
    dev = _config()["project"]["optional-dependencies"]["dev"]
    names = {spec.split("==")[0].split(">")[0].split("[")[0] for spec in dev}
    assert {"pytest", "ruff", "mypy"}.issubset(names), (
        f"missing quality tools in dev extra: {names}"
    )


def test_pytest_and_ruff_and_mypy_are_configured():
    tool = _config()["tool"]
    assert tool["pytest"]["ini_options"]["testpaths"] == ["tests"]
    assert tool["ruff"]["line-length"] == 100
    assert "format" in tool["ruff"]
    # One type checker; the app package is always in scope (other dirs may be added by later stories).
    assert "app" in tool["mypy"]["files"]
