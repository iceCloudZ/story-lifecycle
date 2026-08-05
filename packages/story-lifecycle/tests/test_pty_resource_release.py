"""PTY 资源释放测试（设计 14 §2.3）— spawn 后的收尾契约。

设计 13 编排线程的 stage 收尾（_release_stage / _finalize_stage_pass）必须保证：
- stage 正常退出 → PTY 释放（clean_exit_pty 被调 / kill 兜底）
- stage 异常 → try/finally 仍释放 PTY
- stage 退出后 db.delete_session 被调（无孤儿 session 记录）
- marker 文件被 unlink（死了的会话不能留着当 resume 凭据）
"""

import json
from pathlib import Path

import pytest

from story_lifecycle.infra.db import models as db
from story_lifecycle.orchestrator.scheduler import OrchestratorThread


@pytest.fixture
def _orchestrator():
    thr = OrchestratorThread(poll_interval=0.01)
    yield thr
    thr.stop()
    thr._executor_pool.shutdown(wait=False)


class _FakePty:
    def __init__(self, alive=True, session_id="sess-pty-1"):
        self.alive = alive
        self.session_id = session_id
        self.killed = False
        self.clean_exited = False
        self.story_key = "KILL-1"
        self.stage = "design"
        self.adapter = "claude"

    def kill(self):
        self.killed = True
        self.alive = False

    def write(self, *a, **kw):
        pass


class TestReleaseStage:
    def test_normal_exit_releases_pty(
        self, tmp_path, isolated_story_home, _orchestrator, monkeypatch
    ):
        """stage 正常退出 → clean_exit_pty 被调 + kill 兜底 + 注册表清除。"""
        import story_lifecycle.infra.terminal.pty as pty_mod
        from story_lifecycle.orchestrator.executors import InteractiveStageExecutor

        ws = tmp_path / "ws"
        ws.mkdir()
        db.create_story("REL-1", "释放测试", str(ws), profile="minimal")
        db.update_story(
            "REL-1",
            context_json=json.dumps(
                {
                    "_plan_confirmed": True,
                    "_agent_actions": [
                        {"action": "launch", "stage": "design", "adapter": "claude"}
                    ],
                },
                ensure_ascii=False,
            ),
        )
        fake_pty = _FakePty()
        executor = InteractiveStageExecutor()
        executor._last_pty = fake_pty
        _orchestrator._executors["REL-1"] = executor

        calls = []
        monkeypatch.setattr(pty_mod, "clean_exit_pty", lambda p, **kw: calls.append("clean"))
        monkeypatch.setattr(pty_mod, "kill_pty", lambda *a, **kw: calls.append("kill_pty"))

        _orchestrator._release_stage("REL-1", "design", executor)
        assert "clean" in calls, "clean_exit_pty 应被调（正常退出路径）"
        assert "kill_pty" in calls
        assert fake_pty.killed  # kill 兜底

    def test_exception_still_releases_pty(
        self, tmp_path, isolated_story_home, _orchestrator, monkeypatch
    ):
        """stage 异常（clean_exit_pty 抛）→ try/finally 仍释放 PTY。"""
        import story_lifecycle.infra.terminal.pty as pty_mod
        from story_lifecycle.orchestrator.executors import InteractiveStageExecutor

        ws = tmp_path / "ws"
        ws.mkdir()
        db.create_story("REL-2", "释放异常测试", str(ws), profile="minimal")
        db.update_story(
            "REL-2",
            context_json=json.dumps(
                {
                    "_plan_confirmed": True,
                    "_agent_actions": [
                        {"action": "launch", "stage": "design", "adapter": "claude"}
                    ],
                },
                ensure_ascii=False,
            ),
        )
        fake_pty = _FakePty()
        executor = InteractiveStageExecutor()
        executor._last_pty = fake_pty
        _orchestrator._executors["REL-2"] = executor

        def _boom(*a, **kw):
            raise RuntimeError("clean_exit failed")

        monkeypatch.setattr(pty_mod, "clean_exit_pty", _boom)

        # 不抛：_release_stage 内部 try/except 兜底
        _orchestrator._release_stage("REL-2", "design", executor)
        assert fake_pty.killed, "clean_exit 抛异常后 kill 兜底仍应执行"

    def test_no_pty_is_noop(self, tmp_path, isolated_story_home, _orchestrator):
        """无 PTY → _release_stage 是 no-op（不崩）。"""
        from story_lifecycle.orchestrator.executors import InteractiveStageExecutor

        executor = InteractiveStageExecutor()
        _orchestrator._release_stage("NOPE-1", "design", executor)  # 不抛


class TestDeleteSessionOnExit:
    def test_kill_pty_cleans_db_session(
        self, tmp_path, isolated_story_home, monkeypatch
    ):
        """stage 退出后 db.delete_session 被调（无孤儿 session 记录）。"""
        import story_lifecycle.infra.terminal.pty as pty_mod

        ws = tmp_path / "ws"
        ws.mkdir()
        db.create_story("KILL-1", "清理测试", str(ws), profile="minimal")
        db.upsert_session("KILL-1", "design", "claude", session_id="sess-xyz")

        deleted = []
        monkeypatch.setattr(
            db, "delete_session", lambda *a, **kw: deleted.append(a)
        )
        fake_pty = _FakePty(session_id="sess-pty-1")
        # 把 fake pty 注册进 registry（kill_pty 走真实注册表路径）
        with pty_mod._lock:
            pty_mod._ptys.setdefault("KILL-1", {})["sess-pty-1"] = fake_pty
        try:
            pty_mod.kill_pty("KILL-1", "sess-pty-1")
        finally:
            with pty_mod._lock:
                pty_mod._ptys.pop("KILL-1", None)
        assert fake_pty.killed
        assert deleted, "kill_pty 应清理 DB session（防孤儿 session 记录）"

    def test_stage_finalize_cleans_session(
        self, tmp_path, isolated_story_home, _orchestrator, monkeypatch
    ):
        """_finalize_stage_pass（approve 收尾）→ 记 completed + 释放 PTY。"""
        import story_lifecycle.infra.terminal.pty as pty_mod
        from story_lifecycle.orchestrator.executors import InteractiveStageExecutor

        ws = tmp_path / "ws"
        ws.mkdir()
        db.create_story("FIN-1", "收尾测试", str(ws), profile="minimal")
        db.update_story(
            "FIN-1",
            context_json=json.dumps(
                {
                    "_plan_confirmed": True,
                    "_agent_actions": [
                        {"action": "launch", "stage": "design", "adapter": "claude"}
                    ],
                },
                ensure_ascii=False,
            ),
        )
        calls = []
        monkeypatch.setattr(
            pty_mod, "clean_exit_pty", lambda p, **kw: calls.append("clean")
        )
        fake_pty = _FakePty()
        executor = InteractiveStageExecutor()
        executor._last_pty = fake_pty
        _orchestrator._executors["FIN-1"] = executor
        _orchestrator._finalize_stage_pass(
            "FIN-1",
            "design",
            executor,
            db.get_story("FIN-1"),
            {"stage": "design", "summary": "done"},
        )
        assert "clean" in calls
        events = db.get_story_events("FIN-1")
        assert any(e.get("event_type") == "completed" for e in events)
        assert db.get_session("FIN-1", "design", "claude") is None or not db.get_session(
            "FIN-1", "design", "claude"
        ).get("session_id")


class TestMarkerUnlink:
    def test_dead_pty_clears_marker(
        self, tmp_path, isolated_story_home, monkeypatch
    ):
        """spawn 后 PTY 立即死了 → marker unlink + DB session 清理（下次全新 spawn）。"""
        import story_lifecycle.orchestrator.service.api as api

        ws = tmp_path / "ws"
        ws.mkdir()
        db.create_story("MARK-1", "marker 测试", str(ws), profile="minimal")
        db.update_story(
            "MARK-1",
            context_json=json.dumps(
                {
                    "_plan_confirmed": True,
                    "_agent_actions": [
                        {"action": "launch", "stage": "design", "adapter": "claude"}
                    ],
                },
                ensure_ascii=False,
            ),
        )
        marker = Path(ws) / ".story" / "context" / "MARK-1" / "session_design.json"
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(json.dumps({"session_id": "sess-uuid5"}), encoding="utf-8")
        db.upsert_session("MARK-1", "design", "claude", session_id="sess-uuid5")

        # 模拟 spawn 契约：_spawn_story_agent_pty 走完 → PTY 已死
        class _DeadAdapter:
            name = "claude"
            prespecified_session_id = True
            readiness_marker = None
            default_model = "sonnet"

            def start_session(self, model, prompt="", session_id="", session_name="", resume=False):
                from story_lifecycle.knowledge.adapters.base import SessionSpec

                return SessionSpec(
                    command=["claude-fake"], pty_prompt="", readiness_marker=None
                )

            def make_sid_capturer(self, *a, **k):
                return None

        import story_lifecycle.infra.terminal.pty as pty_mod
        from story_lifecycle.infra.story_paths import build_story_spawn_env

        story = db.get_story("MARK-1")
        # 直接驱动 _spawn_story_agent_pty 的「死了就清」段
        # （1.5s 存活检查 + 清理分支，monkeypatch time.sleep 提速）
        import time as _time

        monkeypatch.setattr(_time, "sleep", lambda _s: None)
        dead_pty = _FakePty(alive=False)

        def _fake_ensure(*a, **kw):
            return ("sess-uuid5", dead_pty)

        monkeypatch.setattr(api, "ensure_agent_pty", _fake_ensure)

        session_id, pty, is_resume = api._spawn_story_agent_pty(story, _DeadAdapter(), "sonnet")
        assert is_resume is False
        assert not marker.exists(), "PTY 死了 → marker 应被 unlink（防 resume 死 sid）"
        row = db.get_session("MARK-1", "design", "claude")
        assert not row or not row.get("session_id"), "PTY 死了 → DB session 应被清理"
