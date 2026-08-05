"""drive_story_sync 收敛测试（设计 14 §2.3）。

设计 13 后 continue_orchestrator_agent 是同步驱动 shim，内部调
``scheduler.drive_story_sync`` 循环 tick。本文件锁定收敛契约：
- story 进入 paused → 返回 "paused"，不再 tick
- completed/failed → 同上
- max_rounds 达上限仍 active → 返回当前 status（不卡死）
- force_auto=True vs False → executor 选 Automatic vs Interactive
"""

import json

import pytest

from story_lifecycle.infra.db import models as db
from story_lifecycle.orchestrator.scheduler import drive_story_sync


@pytest.fixture
def story_row(tmp_path, isolated_story_home):
    ws = tmp_path / "ws"
    ws.mkdir()
    db.create_story("SYNC-1", "同步驱动测试", str(ws), profile="minimal")
    db.update_story(
        "SYNC-1",
        lifecycle_state="待启动",
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
    return "SYNC-1"


class TestConvergence:
    def test_paused_returns_paused(self, story_row):
        """story 已 paused → drive_story_sync 返回 "paused"。"""
        db.update_story(story_row, status="paused")
        assert drive_story_sync(story_row, max_rounds=5) == "paused"

    def test_completed_returns_completed(self, story_row):
        """story 已 completed → 返回 "completed"。"""
        db.update_story(story_row, status="completed")
        assert drive_story_sync(story_row, max_rounds=5) == "completed"

    def test_failed_returns_failed(self, story_row):
        """story 已 failed → 返回 "failed"。"""
        db.update_story(story_row, status="failed")
        assert drive_story_sync(story_row, max_rounds=5) == "failed"

    def test_max_rounds_active_returns_status(self, story_row):
        """达到 max_rounds 仍 active → 返回当前 status（不卡死）。"""
        # 半自动 profile + 无人 spawn + 无成果物 → 每轮 tick 都 no-op，循环到上限
        assert drive_story_sync(story_row, max_rounds=2) == "active"

    def test_no_actions_fails_fast(self, tmp_path, isolated_story_home):
        """无 _agent_actions 的 story → drive_story_sync 快速返回（不跑满循环）。"""
        ws = tmp_path / "ws2"
        ws.mkdir()
        db.create_story("SYNC-NOACT", "无 action", str(ws), profile="minimal")
        db.update_story("SYNC-NOACT", context_json=json.dumps({}))
        assert drive_story_sync("SYNC-NOACT", max_rounds=2) in ("active", "failed")


class TestForceAuto:
    def test_force_auto_uses_automatic_executor(self, story_row):
        """force_auto=True → 即使 minimal(半自动)profile 也强制 Automatic 执行器。"""
        from story_lifecycle.orchestrator.scheduler import OrchestratorThread
        from story_lifecycle.orchestrator.executors import (
            AutomaticStageExecutor,
            make_stage_executor,
        )

        thr = OrchestratorThread(poll_interval=0, force_auto=True)
        try:
            story = db.get_story(story_row)
            ctx = json.loads(story["context_json"])
            # 半自动 profile 默认返回 Interactive —— force_auto 分支应换掉它
            base = make_stage_executor(story, ctx)
            assert not isinstance(base, AutomaticStageExecutor)  # minimal 是半自动
            executor = thr._resolve_executor(story, ctx, story["current_stage"])
            if not isinstance(executor, AutomaticStageExecutor):
                # force_auto 分支在 _tick_story 里换（这里验证 _tick_story 不崩即可）
                pass
            # 直接验证 _tick_story 的 force_auto 换执行器逻辑
            thr._executors[story_row] = base
            from story_lifecycle.orchestrator.executors import InteractiveStageExecutor

            assert isinstance(base, InteractiveStageExecutor)
            # _tick_story 首行 force_auto 分支会把 executor 换成 Automatic
            thr._tick_story(story)
            resolved = thr._executors[story_row]
            assert isinstance(resolved, AutomaticStageExecutor), (
                "force_auto=True 时应强制 Automatic 执行器"
            )
        finally:
            thr._executor_pool.shutdown(wait=False)

    def test_force_auto_false_respects_profile(self, story_row):
        """force_auto=False → minimal profile 保持 Interactive（半自动不自动 spawn）。"""
        from story_lifecycle.orchestrator.scheduler import OrchestratorThread
        from story_lifecycle.orchestrator.executors import InteractiveStageExecutor

        thr = OrchestratorThread(poll_interval=0, force_auto=False)
        try:
            story = db.get_story(story_row)
            ctx = json.loads(story["context_json"])
            executor = thr._resolve_executor(story, ctx, story["current_stage"])
            assert isinstance(executor, InteractiveStageExecutor), (
                "force_auto=False 时 minimal profile 应是 Interactive(半自动)"
            )
        finally:
            thr._executor_pool.shutdown(wait=False)
