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
    """成果物没落地 → planner 不推进,超时后失败。"""
    import time

    _setup_planning(story)
    monkeypatch.setattr(time, "sleep", lambda s: None)  # 加速轮询

    with patch("subprocess.Popen", return_value=_mock_headless_proc()), patch(
        "story_lifecycle.orchestrator.engine.claude_stream.supervise_headless_stdout"
    ), patch("story_lifecycle.orchestrator.engine.planner._kill_headless"):
        continue_orchestrator_agent(story["story_key"], headless=True)

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


# ---- consume_orphan_artifacts(GET /api/story/{key} 被动扫成果物) ----


def test_consume_orphan_artifacts_claims_landed_stage(story, tmp_path):
    """成果物落地但 driver 没在跑 → consume_orphan_artifacts 认领(打开详情页解卡)。"""
    _setup_planning(story)
    # 落地成果物,模拟"code agent 干完了但 driver 挂了"
    spec = tmp_path / "story" / "spec.md"
    spec.parent.mkdir(parents=True, exist_ok=True)
    spec.write_text("# 设计\n", encoding="utf-8")

    # story 不在 running 状态,无 driver_claim → 应被认领
    claimed = graph.consume_orphan_artifacts(story["story_key"])
    assert claimed is True
    updated = db.get_story(story["story_key"])
    assert updated["status"] == "completed"


def test_consume_orphan_artifacts_noop_when_artifacts_missing(story, tmp_path):
    """成果物没落地 → consume_orphan_artifacts 不认领(返回 False)。"""
    _setup_planning(story)
    claimed = graph.consume_orphan_artifacts(story["story_key"])
    assert claimed is False


def test_consume_orphan_done_alias_works(story, tmp_path):
    """consume_orphan_done 是 consume_orphan_artifacts 的向后兼容别名。"""
    _setup_planning(story)
    spec = tmp_path / "story" / "spec.md"
    spec.parent.mkdir(parents=True, exist_ok=True)
    spec.write_text("# 设计\n", encoding="utf-8")
    # 老调用点(api.py)仍调 consume_orphan_done 名字 —— 应工作
    assert graph.consume_orphan_done(story["story_key"]) is True


def test_consume_orphan_artifacts_skipped_when_driver_running(story, tmp_path):
    """driver 在跑 → consume_orphan_artifacts 不抢(让 driver 的 poll loop 管)。"""
    _setup_planning(story)
    spec = tmp_path / "story" / "spec.md"
    spec.parent.mkdir(parents=True, exist_ok=True)
    spec.write_text("# 设计\n", encoding="utf-8")
    # 标 story 为 running(driver 在跑)
    with patch("story_lifecycle.orchestrator.engine.graph.is_story_running", return_value=True):
        claimed = graph.consume_orphan_artifacts(story["story_key"])
    assert claimed is False
