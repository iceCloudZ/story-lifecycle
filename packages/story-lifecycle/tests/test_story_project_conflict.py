"""DB-layer tests for story_project worktree_path conflict resolution (NULL approach).

Covers: NULL placeholder (no _pending_), multiple unprepared bindings coexistence,
stale-occupant displacement, active-occupant 409, and the update path.
"""

import pytest

from story_lifecycle.infra.db import models as db


def _seed_story(key, tmp_path):
    db.create_story(story_key=key, title="t", workspace=str(tmp_path / key))


def _seed_project(name, tmp_path):
    db.create_project(name=name, repo_path=str(tmp_path / name))
    return 1  # first project in an isolated db -> id 1


PATH = "D:/hc-all/hc-user"


# -------- NULL placeholder --------


def test_bind_without_worktree_path_stores_null(isolated_story_home, tmp_path):
    _seed_story("S1", tmp_path)
    _seed_project("p1", tmp_path)
    row = db.bind_story_project("S1", 1, branch="feat")
    assert row["worktree_path"] is None
    assert "_pending_" not in (row["worktree_path"] or "")  # no placeholder regression


def test_multiple_unprepared_bindings_coexist(isolated_story_home, tmp_path):
    """Regression for the original 500: several stories, no worktree_path, must not collide."""
    _seed_project("shared", tmp_path)
    for key in ("A", "B", "C"):
        _seed_story(key, tmp_path)
        row = db.bind_story_project(key, 1, branch=f"feat/{key}")
        assert row["worktree_path"] is None


def test_null_path_never_conflicts_with_real_path(isolated_story_home, tmp_path):
    _seed_project("p", tmp_path)
    _seed_story("A", tmp_path)
    _seed_story("B", tmp_path)
    db.bind_story_project("A", 1, branch="feat/a", worktree_path=PATH, worktree_state="available")
    b = db.bind_story_project("B", 1, branch="feat/b")  # NULL path
    assert b["worktree_path"] is None


# -------- conflict resolution on bind --------


def test_bind_displaces_stale_unprepared_occupant(isolated_story_home, tmp_path):
    _seed_project("p", tmp_path)
    _seed_story("A", tmp_path)
    _seed_story("B", tmp_path)

    a = db.bind_story_project("A", 1, branch="feat/a", worktree_path=PATH)  # default unprepared
    assert a["worktree_state"] == "unprepared"

    b = db.bind_story_project("B", 1, branch="feat/b", worktree_path=PATH)
    assert b["worktree_path"] == PATH
    assert db.get_story_project("A", 1)["worktree_path"] is None  # A displaced


def test_bind_displaces_missing_occupant(isolated_story_home, tmp_path):
    _seed_project("p", tmp_path)
    _seed_story("A", tmp_path)
    _seed_story("B", tmp_path)

    db.bind_story_project("A", 1, branch="feat/a", worktree_path=PATH, worktree_state="missing")
    db.bind_story_project("B", 1, branch="feat/b", worktree_path=PATH)
    assert db.get_story_project("A", 1)["worktree_path"] is None


def test_bind_raises_on_active_occupant(isolated_story_home, tmp_path):
    _seed_project("p", tmp_path)
    _seed_story("A", tmp_path)
    _seed_story("B", tmp_path)

    db.bind_story_project("A", 1, branch="feat/a", worktree_path=PATH, worktree_state="available")
    with pytest.raises(db.WorktreePathConflict) as exc:
        db.bind_story_project("B", 1, branch="feat/b", worktree_path=PATH)
    assert exc.value.occupant["story_key"] == "A"
    assert exc.value.worktree_path == PATH


# -------- conflict resolution on update --------


def test_update_worktree_path_displaces_stale(isolated_story_home, tmp_path):
    _seed_project("p", tmp_path)
    _seed_story("A", tmp_path)
    _seed_story("B", tmp_path)

    db.bind_story_project("A", 1, branch="feat/a", worktree_path=PATH)
    db.bind_story_project("B", 1, branch="feat/b")  # NULL

    db.update_story_project("B", 1, worktree_path=PATH)
    assert db.get_story_project("B", 1)["worktree_path"] == PATH
    assert db.get_story_project("A", 1)["worktree_path"] is None


def test_update_worktree_path_raises_on_active(isolated_story_home, tmp_path):
    _seed_project("p", tmp_path)
    _seed_story("A", tmp_path)
    _seed_story("B", tmp_path)

    db.bind_story_project("A", 1, branch="feat/a", worktree_path=PATH, worktree_state="available")
    db.bind_story_project("B", 1, branch="feat/b")

    with pytest.raises(db.WorktreePathConflict):
        db.update_story_project("B", 1, worktree_path=PATH)


def test_init_db_migrates_legacy_pending_placeholders_to_null(
    isolated_story_home, tmp_path
):
    """Legacy _pending_... rows are nulled by init_db (idempotent one-time migration)."""
    db.create_story(story_key="LEGACY", title="t", workspace=str(tmp_path))
    db.create_project(name="p", repo_path=str(tmp_path / "p"))
    db.bind_story_project("LEGACY", 1, branch="feat")  # stores NULL
    # Simulate a legacy placeholder left by the old code / manual workaround
    with db._db() as conn:
        conn.execute(
            "UPDATE story_project SET worktree_path = '_pending_LEGACY_1' "
            "WHERE story_key = 'LEGACY' AND project_id = 1"
        )
    assert db.get_story_project("LEGACY", 1)["worktree_path"] == "_pending_LEGACY_1"

    db.init_db()  # migration fires

    assert db.get_story_project("LEGACY", 1)["worktree_path"] is None
