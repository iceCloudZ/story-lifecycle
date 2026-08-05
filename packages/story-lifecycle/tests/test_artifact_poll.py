"""1.4 — 成果物驱动推进 + consume_orphan_artifacts 测试。

设计依据:DESIGN-artifact-driven-stage-completion §1.2 / STEP 1.4。
planner poll loop 改查 stage.artifacts 落地(替 done.json 自报);consume_orphan_done
改 consume_orphan_artifacts(GET /api/story/{key} 被动扫成果物)。
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from story_lifecycle.infra.db import models as db
from story_lifecycle.orchestrator.engine import graph
from story_lifecycle.orchestrator.engine.planner import (
    continue_orchestrator_agent,
    run_orchestrator_agent,
)


@pytest.fixture(autouse=True)
def _isolated_evidence_root(tmp_path, monkeypatch):
    """把 evidence 根锁到 workspace/story（不走向上找 .agents 的真实路径）。

    story_evidence_root 会沿父目录找 .agents/AGENTS.md —— 测试 tmp_path 位于
    C:/Users/zzh58 下,命中真实 .agents → evidence 候选指到用户 story 目录
    （被真实跑测污染,spec.md 存在 → is_artifacts_ready 误判 True）。
    """
    import story_lifecycle.infra.story_paths as sp
    from pathlib import Path as _P

    monkeypatch.setattr(
        sp, "story_evidence_root", lambda ws: _P(str(ws or ".")) / "story"
    )


@pytest.fixture
def story(tmp_path):
    return db.create_story(
        story_key="STORY-ART-1",
        title="成果物驱动推进测试",
        workspace=str(tmp_path),
        profile="headless-smoke",
        current_stage="design",
    )


def _make_mock_llm():
    mock_llm = MagicMock()
    mock_llm.api_key = "fake"

    class FakeStage:
        def __init__(self, stage, skip=False, focus="", task_actions=None, grill=False):
            self.stage = stage
            self.skip = skip
            self.focus = focus
            self.task_actions = task_actions or []
            self.grill = grill

    class FakePlanResult:
        def __init__(self, stages):
            self.stages = stages

    mock_llm.invoke_structured.return_value = FakePlanResult(
        [FakeStage("design", skip=False, focus="设计方案")]
    )
    mock_llm.invoke_with_tools = lambda *a, **k: {
        "message": {"role": "assistant", "content": "planning"},
        "tool_calls": [],
    }
    return mock_llm


def _setup_planning(story):
    with patch(
        "story_lifecycle.orchestrator.engine.planner.get_llm",
        return_value=_make_mock_llm(),
    ):
        run_orchestrator_agent(story["story_key"])
    updated = db.get_story(story["story_key"])
    ctx = json.loads(updated.get("context_json", "{}"))
    ctx["_plan_confirmed"] = True
    db.update_story(
        story["story_key"], context_json=json.dumps(ctx, ensure_ascii=False)
    )


def _mock_headless_proc():
    mock_proc = MagicMock()
    mock_proc.poll.return_value = None
    mock_proc.stdin = MagicMock()
    mock_proc.stdout = MagicMock()
    mock_proc.stderr = MagicMock()
    return mock_proc


# ---- planner poll loop 改查成果物 ----


def test_planner_advances_when_artifact_landed(story, tmp_path):
    """成果物(spec.md)落地 → planner 推进 stage 到 completed(不依赖 done.json)。"""
    _setup_planning(story)
    # 落地成果物(headless-smoke design → story/spec.md),不写 done.json
    spec = tmp_path / "story" / "spec.md"
    spec.parent.mkdir(parents=True, exist_ok=True)
    spec.write_text("# 设计\n", encoding="utf-8")

    with patch("subprocess.Popen", return_value=_mock_headless_proc()), patch(
        "story_lifecycle.orchestrator.engine.claude_stream.supervise_headless_stdout"
    ), patch("story_lifecycle.orchestrator.engine.planner._kill_headless"):
        continue_orchestrator_agent(story["story_key"], headless=True)

    updated = db.get_story(story["story_key"])
    assert updated["status"] == "completed"
    # completed 事件 payload 含合成的 files_changed(因没 done 兼容视图)
    events = db.get_recent_quality_events(story["story_key"], ["completed"], limit=1)
    assert len(events) == 1


def test_planner_does_not_advance_without_artifact(story, tmp_path, monkeypatch):
    """成果物没落地 → 编排线程不推进,超时后失败。"""
    import time

    from story_lifecycle.orchestrator.scheduler import OrchestratorThread

    _setup_planning(story)
    # 设计13:超时是真实时钟(STAGE_TIMEOUT, 默认 45min);测试注入短超时 + 真实 sleep。
    monkeypatch.setattr(OrchestratorThread, "STAGE_TIMEOUT", 0.5)

    thr = OrchestratorThread(poll_interval=0)
    try:
        with patch("subprocess.Popen", return_value=_mock_headless_proc()), patch(
            "story_lifecycle.orchestrator.engine.claude_stream.supervise_headless_stdout"
        ), patch("story_lifecycle.orchestrator.engine.planner._kill_headless"):
            for _ in range(30):
                s = db.get_story(story["story_key"])
                if s and s.get("status") in ("paused", "completed", "failed"):
                    break
                thr._tick()
                time.sleep(0.1)  # 真实 sleep:让 STAGE_TIMEOUT(真实时钟)能触发
    finally:
        thr.stop()
        thr._executor_pool.shutdown(wait=False)

    updated = db.get_story(story["story_key"])
    assert updated["status"] == "failed"


def test_planner_reads_done_compat_view_when_present(story, tmp_path):
    """story-tool declare 写的 done.json 兼容视图作 payload 来源(1.5 双写)。

    成果物落地 + done 兼容视图存在 → planner 读兼容视图的 summary/files_changed。
    """
    _setup_planning(story)
    spec = tmp_path / "story" / "spec.md"
    spec.parent.mkdir(parents=True, exist_ok=True)
    spec.write_text("# 设计\n", encoding="utf-8")
    # 模拟 story-tool declare 写的 done 兼容视图
    done_path = tmp_path / ".story" / "done" / story["story_key"] / "design.json"
    done_path.parent.mkdir(parents=True, exist_ok=True)
    done_path.write_text(
        json.dumps(
            {
                "stage": "design",
                "status": "done",
                "summary": "story-tool declare 写的兼容视图",
                "spec_path": "story/spec.md",
                "files_changed": ["story/spec.md"],
            }
        ),
        encoding="utf-8",
    )

    with patch("subprocess.Popen", return_value=_mock_headless_proc()), patch(
        "story_lifecycle.orchestrator.engine.claude_stream.supervise_headless_stdout"
    ), patch("story_lifecycle.orchestrator.engine.planner._kill_headless"):
        continue_orchestrator_agent(story["story_key"], headless=True)

    updated = db.get_story(story["story_key"])
    assert updated["status"] == "completed"
    # completed 事件 payload 应来自兼容视图(summary 是 declare 写的那句)
    events = db.get_recent_quality_events(story["story_key"], ["completed"], limit=1)
    assert len(events) == 1
    payload = json.loads(events[0]["payload"])
    assert "story-tool declare" in payload.get("summary", "")


# ---- 设计13:orphan 成果物由全局编排线程 poll 发现(替代 consume_orphan_artifacts) ----


def test_orchestrator_claims_landed_stage_without_pty(story, tmp_path, monkeypatch):
    """成果物落地但无 PTY(用户手动跑完/driver 曾挂)→ 编排线程 submit judge → 完成。

    设计13 替代 consume_orphan_artifacts(GET /story 副作用):编排线程每轮
    poll artifacts,无 PTY 也能发现落地成果物并推进。
    """
    import time as _time

    from story_lifecycle.orchestrator.scheduler import OrchestratorThread

    _setup_planning(story)
    # 落地成果物,模拟"code agent 干完了但没有任何 PTY 在跑"
    spec = tmp_path / "story" / "spec.md"
    spec.parent.mkdir(parents=True, exist_ok=True)
    spec.write_text("# 设计\n", encoding="utf-8")
    # 加速轮询(judge 子线程在真实线程池里,不需要真实 sleep)
    monkeypatch.setattr(_time, "sleep", lambda s: None)

    thr = OrchestratorThread(poll_interval=0)
    try:
        for _ in range(50):
            s = db.get_story(story["story_key"])
            if s and s.get("status") in ("paused", "completed", "failed"):
                break
            thr._tick()
    finally:
        thr.stop()
        thr._executor_pool.shutdown(wait=False)

    updated = db.get_story(story["story_key"])
    assert updated["status"] == "completed"


def test_orchestrator_does_not_judge_without_artifact(story, tmp_path, monkeypatch):
    """成果物没落地 → 编排线程不推进(等 spawn 的 CLI 产出)。"""
    import time as _time

    from story_lifecycle.orchestrator.scheduler import OrchestratorThread

    _setup_planning(story)
    monkeypatch.setattr(_time, "sleep", lambda s: None)
    # mock Popen:CI 机器上没有真实 claude 二进制,Popen 抛 FileNotFoundError →
    # spawn 标 failed(本地有 claude 才 active)。mock 后行为与环境无关。
    mock_proc = MagicMock()
    mock_proc.poll.return_value = None
    mock_proc.stdin = MagicMock()
    mock_proc.stdout = MagicMock()
    mock_proc.stderr = MagicMock()

    thr = OrchestratorThread(poll_interval=0)
    try:
        with patch("subprocess.Popen", return_value=mock_proc), patch(
            "story_lifecycle.orchestrator.engine.claude_stream.supervise_headless_stdout"
        ), patch("story_lifecycle.orchestrator.engine.planner._kill_headless"):
            thr._tick()
            thr._tick()
    finally:
        thr.stop()
        thr._executor_pool.shutdown(wait=False)

    updated = db.get_story(story["story_key"])
    assert updated["status"] == "active"
    assert "design" not in (
        json.loads(updated.get("context_json", "{}")).get("_completed_stages") or []
    )
