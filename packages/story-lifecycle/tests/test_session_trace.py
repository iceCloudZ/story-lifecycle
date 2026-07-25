"""1.7a — story_session 执行轨迹扩展测试。

设计依据:DESIGN-artifact-driven-stage-completion §4.10。
story_session 加 attempt/outcome/failure_reason/artifacts_prod/pty_log_ref 字段。
update_session_trace 更新这些字段(幂等,只更给定字段)。
"""

from __future__ import annotations

import json

from story_lifecycle.infra.db import models as db


def _seed_session(story_key="STR-1", stage="design", adapter="claude"):
    """建 story + upsert 一条 session 行。"""
    db.create_story(story_key, "t", "/tmp/ws", profile="minimal", current_stage="design")
    db.upsert_session(story_key, stage, adapter, session_id="sid-1")
    return story_key


def test_update_session_trace_sets_fields(tmp_path, monkeypatch):
    monkeypatch.setenv("STORY_HOME", str(tmp_path / ".story-home"))
    db.init_db()
    sk = _seed_session()
    db.update_session_trace(
        sk, "design", "claude",
        attempt=2,
        outcome="failed",
        failure_reason="spec.md 没落地",
        artifacts_prod=["story/spec.md"],
        pty_log_ref="/tmp/ws/.story/runs/STR-1/pty_design",
    )
    row = db.get_session(sk, "design", "claude")
    assert row["attempt"] == 2
    assert row["outcome"] == "failed"
    assert row["failure_reason"] == "spec.md 没落地"
    assert json.loads(row["artifacts_prod"]) == ["story/spec.md"]
    assert row["pty_log_ref"].endswith("pty_design")


def test_update_session_trace_partial_update(tmp_path, monkeypatch):
    """只更给定字段,None 的不动(幂等部分更新)。"""
    monkeypatch.setenv("STORY_HOME", str(tmp_path / ".story-home"))
    db.init_db()
    sk = _seed_session("STR-2")
    # 第一次:全设
    db.update_session_trace(
        sk, "design", "claude",
        attempt=1, outcome="success", failure_reason="",
    )
    # 第二次:只更 attempt(其他不动)
    db.update_session_trace(sk, "design", "claude", attempt=2)
    row = db.get_session(sk, "design", "claude")
    assert row["attempt"] == 2  # 更了
    assert row["outcome"] == "success"  # 没动
    assert row["failure_reason"] == ""  # 没动


def test_update_session_trace_no_fields_noop(tmp_path, monkeypatch):
    """没字段要更新 → 直接返回(不报错)。"""
    monkeypatch.setenv("STORY_HOME", str(tmp_path / ".story-home"))
    db.init_db()
    sk = _seed_session("STR-3")
    # 全 None → 不更,不抛
    db.update_session_trace(sk, "design", "claude")
    row = db.get_session(sk, "design", "claude")
    # 新列默认 NULL
    assert row["attempt"] is None
    assert row["outcome"] is None


def test_update_session_trace_missing_row_silent(tmp_path, monkeypatch):
    """session 行不存在 → 静默 no-op(防御:某些路径 session 未建)。"""
    monkeypatch.setenv("STORY_HOME", str(tmp_path / ".story-home"))
    db.init_db()
    db.create_story("STR-4", "t", "/tmp/ws", profile="minimal")
    # 没有 session 行 → update 0 行,不抛
    db.update_session_trace("STR-4", "design", "claude", attempt=1)
    assert db.get_session("STR-4", "design", "claude") is None


def test_update_session_trace_artifacts_serialized_as_json(tmp_path, monkeypatch):
    """artifacts_prod 传 list → 序列化成 JSON 字符串存。"""
    monkeypatch.setenv("STORY_HOME", str(tmp_path / ".story-home"))
    db.init_db()
    sk = _seed_session("STR-5")
    db.update_session_trace(
        sk, "design", "claude",
        artifacts_prod=["story/spec.md", "story/research.md"],
    )
    row = db.get_session(sk, "design", "claude")
    parsed = json.loads(row["artifacts_prod"])
    assert parsed == ["story/spec.md", "story/research.md"]
