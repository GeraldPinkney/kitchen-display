"""Shared helpers for the kitchen-display test suite.

Tests load production modules by explicit file path (not sys.path) so that a
module in one test cannot shadow another test's import.
"""
import importlib.util
import json
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]


def load_tool(name: str):
    """Import tools/<name>.py by path; fail the test if it does not exist."""
    path = ROOT / "tools" / f"{name}.py"
    if not path.is_file():
        pytest.fail(f"missing {path.relative_to(ROOT)}")
    modname = f"kd_{name}"
    spec = importlib.util.spec_from_file_location(modname, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[modname] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="session")
def repo_root() -> pathlib.Path:
    return ROOT


@pytest.fixture(scope="session")
def live_entity_ids() -> set[str]:
    """Entity ids captured from the live HA instance on 2026-11-16."""
    fixture = ROOT / "tests" / "fixtures" / "ha_states.json"
    return set(json.loads(fixture.read_text()))
