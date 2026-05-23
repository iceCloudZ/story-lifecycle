"""Shared pytest fixtures — isolated DB and graph globals reset."""

import pytest

from story_lifecycle.db import models as db
from story_lifecycle.orchestrator import graph
import story_lifecycle.orchestrator.nodes as nodes_mod


@pytest.fixture(autouse=True)
def _reset_graph_globals():
    """Clear in-process graph state before and after every test."""
    graph._running_stories.clear()
    graph._workspace_locks.clear()
    graph._plan_done.clear()
    graph._terminal_opened.clear()
    with graph._running_lock:
        graph._running_stories.clear()
    yield
    graph._running_stories.clear()
    graph._workspace_locks.clear()
    graph._plan_done.clear()
    graph._terminal_opened.clear()


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

    db.init_db()
    return story_home
