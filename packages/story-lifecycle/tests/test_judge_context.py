"""2.4 — 无状态上下文组装测试。

设计依据:DESIGN §4.6 / §7.7。从 DB 拼 PRD + 成果物 + 执行轨迹 + 决策历史,
裁剪策略防上下文膨胀。缺字段不崩。
"""

from __future__ import annotations

import pytest

from story_lifecycle.infra.db import models as db
from story_lifecycle.orchestrator.context.judge_context import (
    MAX_ARTIFACT_CHARS,
    MAX_DECISIONS,
    assemble_judge_context,
    context_ref,
)


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path, monkeypatch):
    monkeypatch.setenv("STORY_HOME", str(tmp_path / ".story-home"))
    db.init_db()
    db.create_story("JC-1", "t", str(tmp_path), profile="minimal", current_stage="design")


# ---- 完整前情 ----


def test_assemble_returns_all_sections(tmp_path):
    """有 PRD + 成果物 + session + 决策 → 全部组装进来。"""
    db.upsert_story_doc("JC-1", "prd", "# 需求\n实现 X", "intake", "user")
    spec = tmp_path / "story" / "spec.md"
    spec.parent.mkdir(parents=True)
    spec.write_text("# 设计\n实现 X 的方案", encoding="utf-8")
    db.upsert_session("JC-1", "design", "claude", session_id="s1")
    db.update_session_trace("JC-1", "design", "claude", attempt=1, outcome="running")
    db.log_decision("JC-1", "design", "boundary_judge", "approve", reason="ok")

    ctx = assemble_judge_context(
        "JC-1", "design", str(tmp_path), artifacts=["story/spec.md"], adapter="claude"
    )
    assert ctx["story_key"] == "JC-1"
    assert ctx["stage"] == "design"
    assert "实现 X" in ctx["prd"]
    assert len(ctx["artifacts"]) == 1
    assert ctx["artifacts"][0]["path"] == "story/spec.md"
    assert "实现 X 的方案" in ctx["artifacts"][0]["content"]
    assert ctx["execution_trace"]["session"]["adapter"] == "claude"
    assert len(ctx["decision_history"]) == 1
    assert ctx["decision_history"][0]["decision"] == "approve"


# ---- 裁剪策略(§7.7)----


def test_decision_history_truncated_to_max(tmp_path):
    """决策历史 > MAX_DECISIONS → 截到 MAX_DECISIONS 条(防膨胀)。"""
    for i in range(MAX_DECISIONS + 5):
        db.log_decision("JC-1", "design", "boundary_judge", "approve", reason=f"r{i}")
    ctx = assemble_judge_context("JC-1", "design", str(tmp_path), artifacts=[])
    assert len(ctx["decision_history"]) == MAX_DECISIONS
    # 最近的在前(reason 最大的)
    assert ctx["decision_history"][0]["reason"] == f"r{MAX_DECISIONS + 4}"


def test_artifact_content_truncated(tmp_path):
    """单个成果物内容 > MAX_ARTIFACT_CHARS → 截断。"""
    long_spec = tmp_path / "story" / "spec.md"
    long_spec.parent.mkdir(parents=True)
    long_spec.write_text("X" * (MAX_ARTIFACT_CHARS + 500), encoding="utf-8")
    ctx = assemble_judge_context(
        "JC-1", "design", str(tmp_path), artifacts=["story/spec.md"]
    )
    assert len(ctx["artifacts"][0]["content"]) == MAX_ARTIFACT_CHARS


def test_artifact_content_beyond_old_2000_limit_included(tmp_path):
    """回归(2026-08-06 real-run 1068018):>2000 字符的 spec 后段必须可见。

    旧 MAX_ARTIFACT_CHARS=2000 只喂开头,§4 接口契约/§5 核心逻辑全在截断外
    → judge 误 reject 完整 spec。现在默认 30K,一段 6000 字符、关键内容在
    尾部的 spec 应整体进入上下文。
    """
    spec = tmp_path / "story" / "spec.md"
    spec.parent.mkdir(parents=True)
    body = "现状分析\n" + ("代码引用占位\n" * 200)
    tail = "## 5. 核心逻辑\nrepayAmount = unpaid + MIN - available - coupon"
    spec.write_text(body + tail, encoding="utf-8")
    assert len(body) < 30_000  # 保证在默认阈值内,防阈值回落时误报

    ctx = assemble_judge_context(
        "JC-1", "design", str(tmp_path), artifacts=["story/spec.md"]
    )
    content = ctx["artifacts"][0]["content"]
    assert "## 5. 核心逻辑" in content
    assert "repayAmount = unpaid + MIN - available - coupon" in content


def test_git_artifact_excluded_from_content(tmp_path):
    """git 类 artifact 不读文件内容(它查 git status,不是文件)。"""
    ctx = assemble_judge_context(
        "JC-1", "build", str(tmp_path), artifacts=["git", "story/plan.md"]
    )
    # git 不进 artifacts 内容列表
    paths = [a["path"] for a in ctx["artifacts"]]
    assert "git" not in paths


# ---- 缺字段不崩 ----


def test_missing_prd_returns_empty_string(tmp_path):
    """无 PRD → prd="" 不崩。"""
    ctx = assemble_judge_context("JC-2", "design", str(tmp_path), artifacts=[])
    assert ctx["prd"] == ""
    assert ctx["artifacts"] == []


def test_missing_artifact_file_skipped(tmp_path):
    """artifact 路径不存在 → 跳过(不进列表),不崩。"""
    ctx = assemble_judge_context(
        "JC-1", "design", str(tmp_path), artifacts=["story/missing.md"]
    )
    assert ctx["artifacts"] == []


def test_missing_session_returns_empty_trace(tmp_path):
    """无 session → execution_trace.session=None,不崩。"""
    ctx = assemble_judge_context("JC-1", "design", str(tmp_path), artifacts=[])
    assert ctx["execution_trace"]["session"] is None
    assert ctx["execution_trace"]["recent_events"] == []


def test_missing_decisions_returns_empty_list(tmp_path):
    """无决策历史 → decision_history=[],不崩。"""
    ctx = assemble_judge_context("JC-1", "design", str(tmp_path), artifacts=[])
    assert ctx["decision_history"] == []


def test_story_not_in_db_returns_empty_context(tmp_path):
    """story 不在 DB → 全空上下文,不崩。"""
    ctx = assemble_judge_context("NOPE", "design", str(tmp_path), artifacts=[])
    assert ctx["prd"] == ""
    assert ctx["artifacts"] == []
    assert ctx["decision_history"] == []


# ---- 无状态:多次唤起拿到相同前情 ----


def test_multiple_invocations_get_consistent_context(tmp_path):
    """无状态:两次唤起拿到相同上下文(从 DB 拼,不依赖调用间内存)。"""
    db.upsert_story_doc("JC-1", "prd", "需求 X", "intake", "user")
    db.log_decision("JC-1", "design", "boundary_judge", "approve", reason="ok")
    ctx1 = assemble_judge_context("JC-1", "design", str(tmp_path), artifacts=[])
    ctx2 = assemble_judge_context("JC-1", "design", str(tmp_path), artifacts=[])
    assert ctx1 == ctx2


# ---- context_ref ----


def test_context_ref_stable_and_short():
    """context_ref 短 hash,相同上下文 → 相同 ref;不同 → 不同。"""
    ctx1 = {"prd": "A", "artifacts": [{"path": "x"}], "decision_history": []}
    ctx2 = {"prd": "A", "artifacts": [{"path": "x"}], "decision_history": []}
    ctx3 = {"prd": "B", "artifacts": [{"path": "x"}], "decision_history": []}
    ref1 = context_ref(ctx1)
    ref2 = context_ref(ctx2)
    ref3 = context_ref(ctx3)
    assert ref1 == ref2  # 相同上下文
    assert ref1 != ref3  # 不同 PRD
    assert ref1.startswith("ctx:")
    assert len(ref1) == 16  # "ctx:" + 12 hex
