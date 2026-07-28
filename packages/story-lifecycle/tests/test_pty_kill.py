"""Tests for ManagedPty.kill() — process teardown (incl. Windows Job Object)."""

import sys
import time


def test_kill_terminates_spawned_process(tmp_path):
    """kill() must stop the spawned process. On Windows it goes through the
    KILL_ON_JOB_CLOSE Job Object (or taskkill /T fallback); on Unix, killpg."""
    from story_lifecycle.infra.terminal.pty import ManagedPty

    cmd = [sys.executable, "-c", "import time; time.sleep(60)"]
    pty = ManagedPty("kill-test", story_key="S", stage="design", adapter="test", command=cmd, cwd=str(tmp_path), purpose="test")
    try:
        time.sleep(0.8)
        assert pty.alive, "process should be alive right after spawn"
        pty.kill()
        time.sleep(0.8)
        assert not pty.alive, "process should be dead after kill()"
    finally:
        pty.kill()


def test_kill_pty_clears_dangling_session_row(tmp_path):
    """kill_pty must delete the story_session row for the killed session.

    回归:real-run 2026-07-28 tapd-1144381896001066735 — claude 被紧急停止但
    story_session 残留 status=active,下次 spawn 走 resume 传死 sid,claude 立报
    "No conversation found" 秒退。driver 不变式「CLI 生命周期 ⊆ driver 生命周期」
    要求杀进程连带清 DB 记录。
    """
    from story_lifecycle.infra.db import models as db
    from story_lifecycle.infra.terminal.pty import spawn_pty, kill_pty

    story_key = "kill-session-test"
    # 模拟 spawn 前的 upsert(claude 路径:spawn 前就给 sid)
    db.upsert_session(story_key, "design", "claude", session_id="test-sid-1")
    assert db.get_session(story_key, "design", "claude") is not None

    cmd = [sys.executable, "-c", "import time; time.sleep(60)"]
    session_id, pty = spawn_pty(
        story_key, "design", "claude", cmd, str(tmp_path), purpose="test"
    )
    try:
        time.sleep(0.8)
        assert pty.alive
    finally:
        # kill_pty 按 (story, session_id) 杀单条
        kill_pty(story_key, session_id)
        time.sleep(0.5)

    # 关键断言:DB 悬空记录必须被清(回归核心)
    assert db.get_session(story_key, "design", "claude") is None
    assert not pty.alive


def test_kill_pty_all_clears_all_session_rows(tmp_path):
    """kill_pty(story_id) 全量杀 → 该 story 全部 story_session 行清掉。"""
    from story_lifecycle.infra.db import models as db
    from story_lifecycle.infra.terminal.pty import spawn_pty, kill_pty

    story_key = "kill-all-test"
    db.upsert_session(story_key, "design", "claude", session_id="sid-a")
    db.upsert_session(story_key, "design", "kimi", session_id="sid-b")
    assert len(db.list_sessions_for_story(story_key)) == 2

    cmd = [sys.executable, "-c", "import time; time.sleep(60)"]
    spawn_pty(story_key, "design", "claude", cmd, str(tmp_path), purpose="test")
    try:
        time.sleep(0.8)
    finally:
        kill_pty(story_key)  # 不指定 session_id → 全 story

    # 两条 DB 记录全清
    assert db.list_sessions_for_story(story_key) == []
