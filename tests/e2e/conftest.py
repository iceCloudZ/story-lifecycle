"""E2E test fixtures — scenario loading and workspace setup."""

from pathlib import Path

import pytest

from .scenario import Scenario

SCENARIOS_DIR = Path(__file__).parent / "scenarios"


@pytest.fixture
def e2e_workspace(tmp_path):
    """Provide a clean workspace directory for E2E tests."""
    ws = tmp_path / "workspace"
    ws.mkdir()
    return ws


@pytest.fixture
def scenarios_dir():
    """Return path to the scenarios directory."""
    return SCENARIOS_DIR
