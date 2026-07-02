"""Shared pytest fixtures — isolated DB and graph globals reset."""

import pytest

from story_lifecycle.infra.db import models as db
from story_lifecycle.orchestrator.engine import graph
import story_lifecycle.orchestrator.nodes as nodes_mod
import story_lifecycle.orchestrator.engine.profile_loader as _pl


@pytest.fixture(autouse=True)
def _reset_graph_globals():
    """Clear in-process graph state before and after every test."""
    # Release any file locks held by this process
    import glob as glob_mod

    for lock_file in glob_mod.glob(str(graph._workspace_locks_dir / "*.lock")):
        try:
            from filelock import FileLock

            lock = FileLock(lock_file, timeout=0)
            if lock.is_locked:
                lock.release()
        except Exception:
            pass
    # Clear in-process workspace locks
    for ws, lock in list(graph._ws_inproc_locks.items()):
        if lock.locked():
            lock.release()
    graph._ws_inproc_locks.clear()
    graph._ws_file_locks.clear()
    with graph._running_lock:
        graph._running_stories.clear()
        graph._story_epochs.clear()
    yield
    for lock_file in glob_mod.glob(str(graph._workspace_locks_dir / "*.lock")):
        try:
            from filelock import FileLock

            lock = FileLock(lock_file, timeout=0)
            if lock.is_locked:
                lock.release()
        except Exception:
            pass
    for ws, lock in list(graph._ws_inproc_locks.items()):
        if lock.locked():
            lock.release()
    graph._ws_inproc_locks.clear()
    graph._ws_file_locks.clear()
    with graph._running_lock:
        graph._running_stories.clear()
        graph._story_epochs.clear()
    graph._compiled_graph = None


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path, monkeypatch):
    """Auto-redirect the story DB to a per-test tmp dir so no test ever writes the real
    ~/.story-lifecycle/story.db. Tests that need a populated DB can still request
    isolated_story_home (which also inits tables)."""
    story_home = tmp_path / "story-home"
    story_home.mkdir()
    db_path = story_home / "story.db"
    monkeypatch.setattr(db, "get_db_path", lambda: db_path)
    monkeypatch.setattr(nodes_mod, "STORY_HOME", story_home)
    monkeypatch.setenv("STORY_HOME", str(story_home))
    db.init_db()


@pytest.fixture
def isolated_story_home(_isolated_db, monkeypatch):
    """Isolated home (DB already redirected by _isolated_db autouse) +
    force package built-in profiles."""

    # Force load_profile to always use package built-in profiles,
    # preventing tests from accidentally loading repo-root .story/ profiles
    def _load_builtin_only(name: str) -> dict:
        import importlib.resources as _ir

        try:
            ref = _ir.files("story_lifecycle.entry.profiles").joinpath(f"{name}.yaml")
            return __import__("yaml").safe_load(ref.read_text(encoding="utf-8"))
        except (FileNotFoundError, TypeError):
            pass
        raise FileNotFoundError(f"Profile not found: {name}")

    # Patch at both the module re-export AND the direct source,
    # since graph_nodes.py imports directly from .profile_loader
    monkeypatch.setattr(nodes_mod, "load_profile", _load_builtin_only)
    monkeypatch.setattr(_pl, "load_profile", _load_builtin_only)

    # _isolated_db already set up the tmp story_home and initialized tables.
    # Reconstruct the same path so callers that use the return value keep working.
    import os

    return __import__("pathlib").Path(os.environ["STORY_HOME"])
