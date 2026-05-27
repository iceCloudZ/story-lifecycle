"""Shared pytest fixtures — isolated DB and graph globals reset."""

import pytest

from story_lifecycle.db import models as db
from story_lifecycle.orchestrator import graph
import story_lifecycle.orchestrator.nodes as nodes_mod


@pytest.fixture(autouse=True)
def _reset_graph_globals():
    """Clear in-process graph state before and after every test."""
    graph._workspace_locks.clear()
    graph._plan_done.clear()
    graph._terminal_opened.clear()
    with graph._running_lock:
        graph._running_stories.clear()
        graph._story_epochs.clear()
    yield
    graph._workspace_locks.clear()
    graph._plan_done.clear()
    graph._terminal_opened.clear()
    with graph._running_lock:
        graph._running_stories.clear()
        graph._story_epochs.clear()


@pytest.fixture
def isolated_story_home(tmp_path, monkeypatch):
    """Provide an isolated ~/.story-lifecycle directory for testing."""
    story_home = tmp_path / "story-home"
    story_home.mkdir()
    db_path = story_home / "story.db"
    checkpoint_path = story_home / "checkpoint.db"

    monkeypatch.setattr(db, "get_db_path", lambda: db_path)
    monkeypatch.setattr(graph, "checkpoint_db", checkpoint_path)
    monkeypatch.setattr(nodes_mod, "STORY_HOME", story_home)

    # Force load_profile to always use package built-in profiles,
    # preventing tests from accidentally loading repo-root .story/ profiles
    _orig_load = nodes_mod.load_profile

    def _load_builtin_only(name: str) -> dict:
        import importlib.resources as _ir

        try:
            ref = _ir.files("story_lifecycle.profiles").joinpath(f"{name}.yaml")
            return __import__("yaml").safe_load(ref.read_text(encoding="utf-8"))
        except (FileNotFoundError, TypeError):
            pass
        raise FileNotFoundError(f"Profile not found: {name}")

    monkeypatch.setattr(nodes_mod, "load_profile", _load_builtin_only)

    db.init_db()
    return story_home
