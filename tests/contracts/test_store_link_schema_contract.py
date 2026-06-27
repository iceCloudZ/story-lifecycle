"""Contract test: miner.store schema is compatible with miner.link expectations.

store.py creates the SQLite schema; link.py queries it.  This test ensures
link's required columns/tables exist after store initialization, decoupling
the two modules at the schema level.
"""
import sqlite3
import tempfile

from miner import store
from miner import story_ingest
from miner import link
import os  # noqa: E402


def _in_memory_db():
    """Initialize both store and story_ingest schemas in a temp file."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    store.init_db(path)
    story_ingest.init_db(path)
    return path


def test_link_required_tables_exist_after_init():
    """store + story_ingest must create the tables that link queries."""
    db_path = _in_memory_db()
    with sqlite3.connect(db_path) as conn:
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
    assert "sessions" in tables
    assert "stories" in tables
    assert "events" in tables
    assert "sources" in tables


def test_link_required_columns_exist_after_init():
    """store + story_ingest must create the columns that link reads/writes."""
    db_path = _in_memory_db()
    with sqlite3.connect(db_path) as conn:
        session_cols = {row[1] for row in conn.execute("PRAGMA table_info(sessions)")}
        story_cols = {row[1] for row in conn.execute("PRAGMA table_info(stories)")}

    required_session_cols = {
        "sid", "src", "ws", "ts", "title", "turns", "ntools",
        "nerrs", "first_ucmd", "cwd", "branch",
    }
    required_story_cols = {"story_id", "workspace", "first_ts", "last_ts"}

    assert required_session_cols.issubset(session_cols)
    assert required_story_cols.issubset(story_cols)


def test_link_can_alter_add_story_id_column():
    """link expects to be able to add sessions.story_id if missing."""
    db_path = _in_memory_db()
    with sqlite3.connect(db_path) as conn:
        conn.execute("ALTER TABLE sessions ADD COLUMN story_id TEXT")
        cols = {row[1] for row in conn.execute("PRAGMA table_info(sessions)")}
        assert "story_id" in cols
