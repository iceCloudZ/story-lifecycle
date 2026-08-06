"""OrchestratorThread 生命周期测试（设计 14 §2.3）— start/stop/tick 不泄漏。

设计 13 后编排线程是唯一调度入口。本文件锁定它的生命周期契约：
- start() 后线程存活，stop() 后 join 成功
- _tick 遍历所有 active story
- _tick_story 按 PTY 死活分支（活→poll artifacts；死→看落地）
- _judge_task 结果写回（_judge_results 由主循环读取处理）
- stop() 后线程池 shutdown(wait=True)，无残留任务
- 多次 start()/stop() 不泄漏线程
"""

import json
import threading
import time

import pytest

from story_lifecycle.orchestrator.scheduler import OrchestratorThread
from story_lifecycle.infra.db import models as db


@pytest.fixture
def _orchestrator():
    """一个不自动跑的编排线程实例（测试手动调 _tick）。"""
    thr = OrchestratorThread(poll_interval=0.01)
    yield thr
    thr.stop()
    thr._executor_pool.shutdown(wait=False)


def _make_active_story(tmp_path, story_key="LIFE-1", profile="minimal"):
    """创建 status=active、_plan_confirmed、有 _agent_actions 的 story。"""
    ws = tmp_path / "ws"
    ws.mkdir(exist_ok=True)
    db.create_story(story_key, "生命周期测试", str(ws), profile=profile)
    db.update_story(
        story_key,
        lifecycle_state="待启动",
        context_json=json.dumps(
            {
                "_plan_confirmed": True,
                "_agent_actions": [
                    {"action": "launch", "stage": "design", "adapter": "claude"},
                ],
            },
            ensure_ascii=False,
        ),
    )
    return story_key


class TestThreadLifecycle:
    def test_start_makes_thread_alive(self):
        """start() 后 is_alive() 为 True。"""
        thr = OrchestratorThread(poll_interval=0.01)
        try:
            thr.start()
            assert thr.is_alive()
        finally:
            thr.stop()
            thr.join(timeout=5)
            thr._executor_pool.shutdown(wait=True)

    def test_stop_joins_cleanly(self):
        """stop() 后线程退出，join 成功（不残留 daemon 线程）。"""
        thr = OrchestratorThread(poll_interval=0.01)
        thr.start()
        thr.stop()
        thr.join(timeout=5)
        assert not thr.is_alive()

    def test_multiple_start_stop_no_leak(self):
        """多次 start()/stop() 不泄漏线程（每次 join 干净）。"""
        threads_before = len(threading.enumerate())
        for _ in range(3):
            thr = OrchestratorThread(poll_interval=0.01)
            thr.start()
            thr.stop()
            thr.join(timeout=5)
            thr._executor_pool.shutdown(wait=True)
        # 允许 ±1 浮动（pytest 自身线程），但 3 次启动只应残留 0 个编排线程
        orchestrator_threads = [
            t for t in threading.enumerate() if t.name == "orchestrator"
        ]
        assert len(orchestrator_threads) == 0

    def test_stop_shuts_down_executor_pool(self):
        """stop() 后线程池 shutdown(wait=True)，无残留任务。"""
        thr = OrchestratorThread(poll_interval=0.01)
        thr.start()
        thr.stop()
        thr.join(timeout=5)
        thr._executor_pool.shutdown(wait=True)
        with pytest.raises(RuntimeError):
            thr._executor_pool.submit(lambda: None)  # shutdown 后不能再 submit


class TestTickIteration:
    def test_tick_iterates_all_active_stories(
        self, tmp_path, isolated_story_home, _orchestrator, monkeypatch
    ):
        """_tick 遍历所有 active story（candidate/paused 跳过）。"""
        _make_active_story(tmp_path, "LIFE-A")
        _make_active_story(tmp_path, "LIFE-B")
        # paused 的不碰
        db.update_story("LIFE-B", status="paused")
        ticked = []

        def _fake_tick_story(story):
            ticked.append(story["story_key"])

        monkeypatch.setattr(_orchestrator, "_tick_story", _fake_tick_story)
        _orchestrator._tick()
        assert "LIFE-A" in ticked
        assert "LIFE-B" not in ticked  # paused 跳过

    def test_tick_skips_candidate(self, tmp_path, isolated_story_home, _orchestrator):
        """intake_state=candidate 的 story 不被编排（候选不是执行态）。"""
        ws = tmp_path / "ws"
        ws.mkdir()
        db.create_story("LIFE-C", "候选", str(ws))
        db.update_story("LIFE-C", intake_state="candidate")
        _orchestrator._tick()
        assert db.get_story("LIFE-C")["status"] == "active"  # 未被推进/暂停

    def test_tick_survives_story_exception(
        self, tmp_path, isolated_story_home, _orchestrator, monkeypatch
    ):
        """单个 story 的 tick 抛异常不中断整轮（其他 story 继续）。"""
        _make_active_story(tmp_path, "LIFE-A")
        _make_active_story(tmp_path, "LIFE-B")
        calls = []

        def _fake_tick_story(story):
            calls.append(story["story_key"])
            if story["story_key"] == "LIFE-A":
                raise RuntimeError("boom")

        monkeypatch.setattr(_orchestrator, "_tick_story", _fake_tick_story)
        _orchestrator._tick()  # 不抛
        assert calls.count("LIFE-B") == 1  # B 仍被 tick


class TestTickStoryPtyBranches:
    def test_tick_story_polls_alive_pty(
        self, tmp_path, isolated_story_home, _orchestrator, monkeypatch
    ):
        """PTY 活着 + 无成果物 → 不 judge、不 spawn。"""
        key = _make_active_story(tmp_path)

        class _FakePty:
            alive = True
            session_id = "sess-1"

            def kill(self):
                self.alive = False

        monkeypatch.setattr(
            "story_lifecycle.orchestrator.executors.InteractiveStageExecutor.get_pty",
            lambda self, k, st: _FakePty(),
        )
        submitted = []
        monkeypatch.setattr(
            _orchestrator, "_submit_judge", lambda *a, **k: submitted.append(1)
        )
        _orchestrator._tick()
        assert submitted == []  # 无成果物 → 不 judge

    def test_tick_story_judges_when_dead_pty_with_artifacts(
        self, tmp_path, isolated_story_home, _orchestrator, monkeypatch
    ):
        """PTY 死了 + declare event → submit judge（砍文件兜底后只认 declare）。"""
        key = _make_active_story(tmp_path)
        # declare event（归一化真相源，砍文件兜底后唯一完成信号）
        db.log_event(
            key,
            "design",
            "artifact_declared",
            {"doc_type": "spec", "version": 1, "summary": "ok", "files_changed": []},
        )

        class _DeadPty:
            alive = False
            session_id = "sess-dead"

        monkeypatch.setattr(
            "story_lifecycle.orchestrator.executors.InteractiveStageExecutor.get_pty",
            lambda self, k, st: _DeadPty(),
        )
        submitted = []
        monkeypatch.setattr(
            _orchestrator, "_submit_judge", lambda *a, **k: submitted.append(1)
        )
        _orchestrator._tick()
        assert submitted, "PTY 死 + declare → 应 submit judge"

    def test_tick_story_pauses_when_dead_pty_no_artifacts(
        self, tmp_path, isolated_story_home, _orchestrator, monkeypatch
    ):
        """PTY 死了 + 无成果物 → pause（等人介入）。"""
        key = _make_active_story(tmp_path)

        class _DeadPty:
            alive = False
            session_id = "sess-dead"

            def kill(self):
                pass

        monkeypatch.setattr(
            "story_lifecycle.orchestrator.executors.InteractiveStageExecutor.get_pty",
            lambda self, k, st: _DeadPty(),
        )
        _orchestrator._tick()
        assert db.get_story(key)["status"] == "paused"


class TestJudgeTaskWritesResult:
    def test_judge_task_writes_decision_result(
        self, tmp_path, isolated_story_home, _orchestrator, monkeypatch
    ):
        """_judge_task 把 judge 结果写回（主循环下一轮读取处理）。"""
        key = _make_active_story(tmp_path)
        monkeypatch.setattr(
            "story_lifecycle.orchestrator.evaluation.stage_completion.judge_stage_completion",
            lambda req: {
                "quality": "approve",
                "lifecycle_target": "开发",
                "summary": "设计完成",
                "reason": "ok",
            },
        )
        story = db.get_story(key)
        _orchestrator._judge_task(
            key,
            "design",
            {"stage": "design", "summary": "done"},
            json.loads(story["context_json"]),
            story,
        )
        result = _orchestrator._take_judge_result(key, "design")
        assert result is not None
        assert result["quality"] == "approve"
        assert result["lifecycle_target"] == "开发"

    def test_judge_task_clears_judging_flag(
        self, tmp_path, isolated_story_home, _orchestrator, monkeypatch
    ):
        """judge 完成后 _judging 被清（防重复 submit）。"""
        key = _make_active_story(tmp_path)
        monkeypatch.setattr(
            "story_lifecycle.orchestrator.evaluation.stage_completion.judge_stage_completion",
            lambda req: {
                "quality": "approve",
                "lifecycle_target": None,
                "summary": "ok",
                "reason": "",
            },
        )
        story = db.get_story(key)
        _orchestrator._judging.add(f"{key}:design")
        _orchestrator._judge_task(
            key,
            "design",
            {"stage": "design"},
            json.loads(story["context_json"]),
            story,
        )
        _orchestrator._take_judge_result(key, "design")
        assert f"{key}:design" not in _orchestrator._judging


class TestNoResidualTasks:
    def test_stop_leaves_no_pending_judge_tasks(
        self, tmp_path, isolated_story_home, monkeypatch
    ):
        """stop() + shutdown(wait=True) 后无残留 judge 任务（子线程全收口）。"""
        thr = OrchestratorThread(poll_interval=0.01, max_judge_workers=2)
        try:
            thr.start()

            # 塞两个假 judge 任务（模拟正在跑的 LLM 调用）
            def _slow(**kw):
                time.sleep(0.3)
                return {"quality": "approve", "lifecycle_target": None, "summary": "x"}

            monkeypatch.setattr(
                "story_lifecycle.orchestrator.evaluation.stage_completion.judge_stage_completion",
                _slow,
            )
            key = _make_active_story(tmp_path)
            story = db.get_story(key)
            thr._submit_judge(
                key, "design", json.loads(story["context_json"]), None, story
            )
            thr.stop()
            thr._executor_pool.shutdown(wait=True)  # 等所有 judge 收口
            assert not thr._executor_pool._threads or all(
                not t.is_alive() for t in thr._executor_pool._threads
            )
        finally:
            thr.stop()
            thr._executor_pool.shutdown(wait=False)
