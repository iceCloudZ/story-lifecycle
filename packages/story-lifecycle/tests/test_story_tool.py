"""1.3 — story-tool declare 测试(原子写 + 版本化 + done.json 兼容视图 + 触发感知)。

设计依据:DESIGN §4.4 / §2.3(原子写) / §4.6(miner 双写兼容)。红线:
  - 原子写与存在检查同批(无半成品竞态)
  - miner 双写(declare 同时写 done.json 兼容视图)
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from story_lifecycle.orchestrator.engine import artifact_declare
from story_lifecycle.orchestrator.engine.artifact_declare import declare_artifact
from story_lifecycle.entry.cli.story_tool import _build_context_brief


# ---- helpers ----


def _make_fake_db():
    """Fake db module:记 upsert_story_doc 调用 + 模拟版本+1。"""
    db = MagicMock()
    db._versions = {}

    def _upsert(story_key, doc_type, content, change_reason, author="ai", title=""):
        v = db._versions.get((story_key, doc_type), 0) + 1
        db._versions[(story_key, doc_type)] = v
        return v

    db.upsert_story_doc.side_effect = _upsert
    db.log_event.return_value = None
    db.set_story_doc_local_path.return_value = None
    return db


def _ctx(tmp_path, story_key="STORY-1", stage="design", title="测试"):
    return {
        "story_key": story_key,
        "stage": stage,
        "workspace": str(tmp_path),
        "title": title,
    }


# ---- 原子写:无半成品竞态 ----


def test_declare_with_content_atomically_writes_file(tmp_path):
    """给 content → 原子写到 path,文件出现就是完整内容(无半成品)。"""
    db = _make_fake_db()
    result = declare_artifact(
        "spec",
        "story/spec.md",
        content="# 设计方案\n...",
        summary="登录方案",
        ctx=_ctx(tmp_path),
        db_module=db,
    )
    spec = tmp_path / "story" / "spec.md"
    assert spec.read_text(encoding="utf-8") == "# 设计方案\n..."
    assert result["atomic"] is True


def test_declare_no_half_written_file_visible(tmp_path):
    """红线:原子写期间读者看不到半成品 —— 写完后才看到完整内容。

    用一个慢 content(monkeypatch time.sleep 在 tmp 写后 rename 前)证明:
    并发线程在写过程中读 path 要么看到旧版要么看不到,绝看不到部分内容。
    """
    db = _make_fake_db()
    spec = tmp_path / "story" / "spec.md"
    seen_during_write = []

    # 在 atomic_write 的 tmp 写之后、rename 之前注入观察:此时 final 不应存在。
    real_atomic_write = artifact_declare.atomic_write

    def _spy_atomic_write(path, content):
        # 调真原子写,但在它返回前(final 已可见)我们已在写中途观察过
        # 用一个 hook:写 tmp 后、rename 前观察 final
        from pathlib import Path

        final = Path(path)
        final.parent.mkdir(parents=True, exist_ok=True)
        # 手动模拟 tmp→rename 之间观察 final
        tmp = final.with_name(".probe.tmp")
        tmp.write_text("partial", encoding="utf-8")
        seen_during_write.append(final.exists())  # rename 前 final 应不存在
        tmp.unlink()
        return real_atomic_write(path, content)

    artifact_declare.atomic_write = _spy_atomic_write
    try:
        declare_artifact(
            "spec",
            "story/spec.md",
            content="# 完整方案\n",
            ctx=_ctx(tmp_path),
            db_module=db,
        )
    finally:
        artifact_declare.atomic_write = real_atomic_write

    # 写过程中 final 文件不存在(还没 rename)→ 读者看不到半成品。
    # (declare 触发两次 atomic_write:成果物 + done.json 兼容视图,每次都观察)
    assert seen_during_write, "spy 未触发"
    assert all(seen is False for seen in seen_during_write), (
        f"写过程中 final 已可见(半成品竞态): {seen_during_write}"
    )
    # 写完后 final 存在且完整
    assert spec.read_text(encoding="utf-8") == "# 完整方案\n"


# ---- 版本化:story_doc 版本+1 ----


def test_declare_increments_story_doc_version(tmp_path):
    db = _make_fake_db()
    ctx = _ctx(tmp_path)
    r1 = declare_artifact("spec", "story/spec.md", content="v1", ctx=ctx, db_module=db)
    r2 = declare_artifact("spec", "story/spec.md", content="v2", ctx=ctx, db_module=db)
    assert r1["version"] == 1
    assert r2["version"] == 2
    assert db.upsert_story_doc.call_count == 2


def test_declare_registers_existing_file_no_content(tmp_path):
    """只登记已存在文件(不给 content)→ 读文件内容进 story_doc。"""
    db = _make_fake_db()
    spec = tmp_path / "story" / "spec.md"
    spec.parent.mkdir(parents=True)
    spec.write_text("# 已有方案\n", encoding="utf-8")
    result = declare_artifact("spec", "story/spec.md", ctx=_ctx(tmp_path), db_module=db)
    assert result["version"] == 1
    # upsert 收到的 content 是文件内容
    _args, kwargs = db.upsert_story_doc.call_args
    assert "# 已有方案" in _args[2]  # 第三个位置参是 content


def test_declare_nonexistent_file_without_content_raises(tmp_path):
    """只登记但文件不存在 → FileNotFoundError(不让 code agent 登记空气)。"""
    db = _make_fake_db()
    with pytest.raises(FileNotFoundError):
        declare_artifact(
            "spec",
            "story/missing.md",
            ctx=_ctx(tmp_path),
            db_module=db,
        )


# ---- miner 双写兼容(1.5 红线):done.json 兼容视图 ----


def test_declare_writes_done_compat_view(tmp_path):
    """红线(miner 双写):declare 同时写 .story/done/<key>/<stage>.json 兼容视图。"""
    db = _make_fake_db()
    result = declare_artifact(
        "spec",
        "story/spec.md",
        content="# 方案\n",
        summary="登录方案",
        ctx=_ctx(tmp_path, story_key="STORY-1", stage="design"),
        db_module=db,
    )
    done_path = tmp_path / ".story" / "done" / "STORY-1" / "design.json"
    assert done_path.exists(), "done.json 兼容视图必须写出(miner 双写)"
    data = json.loads(done_path.read_text(encoding="utf-8"))
    # story_ingest 读的字段必须齐
    assert data["stage"] == "design"
    assert data["status"] == "done"
    assert data["summary"] == "登录方案"
    assert "spec_path" in data  # design.json 的 spec_path(story_ingest 显式读)
    assert "files_changed" in data
    assert result["done_view"] == str(done_path)


def test_done_compat_view_has_stage_mtime_signal(tmp_path):
    """miner story_ingest 用 done/<stage>.json 文件存在性取 mtime → 必须存在。"""
    import os

    db = _make_fake_db()
    declare_artifact(
        "test_report",
        "story/test-report.md",
        content="测过了\n",
        ctx=_ctx(tmp_path, story_key="S2", stage="verify"),
        db_module=db,
    )
    done_path = tmp_path / ".story" / "done" / "S2" / "verify.json"
    assert done_path.exists()
    # mtime 可读(story_ingest os.path.getmtime)
    mt = os.path.getmtime(done_path)
    assert mt > 0


# ---- 触发编排器感知 ----


def test_declare_logs_artifact_declared_event(tmp_path):
    db = _make_fake_db()
    declare_artifact(
        "spec", "story/spec.md", content="x", ctx=_ctx(tmp_path), db_module=db
    )
    db.log_event.assert_called_once()
    _args = db.log_event.call_args[0]
    event_type = _args[2]
    payload = _args[3]
    assert event_type == "artifact_declared"
    assert payload["doc_type"] == "spec"
    assert payload["path"]


# ---- rename 失败降级 ----


def test_declare_falls_back_to_nonatomic_on_rename_failure(tmp_path, monkeypatch):
    """rename 全失败 → 降级直接写,标 atomic=false(设计 §7.5 人确认兜底)。"""
    from story_lifecycle.infra import atomic_write as aw

    db = _make_fake_db()
    # 让所有 rename 路径抛 OSError(模拟杀软永久占用)
    monkeypatch.setattr(aw, "_replace_with_movefileex", lambda s, d: (_ for _ in ()).throw(OSError("locked")))
    monkeypatch.setattr(aw.os, "replace", lambda s, d: (_ for _ in ()).throw(OSError("locked")))
    monkeypatch.setattr(aw.time, "sleep", lambda x: None)  # 跳过退避等待

    result = declare_artifact(
        "spec",
        "story/spec.md",
        content="# 方案\n",
        ctx=_ctx(tmp_path),
        db_module=db,
    )
    assert result["atomic"] is False
    # 文件仍然写出了(降级直接写)
    assert (tmp_path / "story" / "spec.md").read_text(encoding="utf-8") == "# 方案\n"


# ---- 上下文缺失 ----


def test_declare_without_story_context_raises(tmp_path, monkeypatch):
    """缺 STORY_KEY/STORY_STAGE 且没 ctx → ValueError(防 code agent 误调)。"""
    monkeypatch.delenv("STORY_KEY", raising=False)
    monkeypatch.delenv("STORY_STAGE", raising=False)
    monkeypatch.delenv("STORY_WORKSPACE", raising=False)
    db = _make_fake_db()
    with pytest.raises(ValueError, match="story 上下文"):
        declare_artifact("spec", "story/spec.md", content="x", db_module=db)


# ---- 环境变量上下文 ----


def test_declare_reads_context_from_env(tmp_path, monkeypatch):
    """ctx=None 时从 STORY_KEY/STORY_STAGE/STORY_WORKSPACE 读(planner spawn 注入)。"""
    monkeypatch.setenv("STORY_KEY", "ENV-STORY")
    monkeypatch.setenv("STORY_STAGE", "build")
    monkeypatch.setenv("STORY_WORKSPACE", str(tmp_path))
    monkeypatch.setenv("STORY_TITLE", "环境")
    db = _make_fake_db()
    result = declare_artifact(
        "plan", "story/plan.md", content="# 计划\n", db_module=db
    )
    assert result["story_key"] == "ENV-STORY"
    assert result["stage"] == "build"


# ---- story tool context:任务简报(纯函数 _build_context_brief)----


def _story_row(
    tmp_path,
    *,
    story_key="S1",
    title="测试 story",
    profile="minimal",
    current_stage="design",
    context_json=None,
):
    """构造 db.get_story() 风格的 story 行。context_json 传 str 原样,否则 json.dumps。"""
    cj = context_json if isinstance(context_json, str) else json.dumps(context_json or {})
    return {
        "story_key": story_key,
        "title": title,
        "profile": profile,
        "workspace": str(tmp_path),
        "current_stage": current_stage,
        "context_json": cj,
    }


def test_context_brief_has_all_sections(tmp_path):
    """PRD/接手说明/本阶段任务俱全时,简报含各段 + 末尾 todo/declare 指针。"""
    prd = tmp_path / "prd.md"
    prd.write_text("# 需求\n实现版本限制功能", encoding="utf-8")
    ctx = {
        "prd_path": str(prd),
        "seed_context": "已做到 spec,还差测试",
        "_agent_actions": [
            {
                "action": "launch",
                "stage": "design",
                "focus": "写设计文档",
                "task_actions": [
                    {"key": "write_design_doc", "description": "产出 spec.md"}
                ],
            }
        ],
    }
    out = _build_context_brief(_story_row(tmp_path, context_json=ctx), "design")
    assert "任务简报" in out
    assert "S1" in out and "测试 story" in out
    assert "design" in out
    assert "实现版本限制功能" in out  # PRD 摘要
    assert "已有工作(接手)" in out and "已做到 spec" in out
    assert "写设计文档" in out  # focus
    assert "write_design_doc" in out  # task_actions
    assert "story tool todo" in out and "story tool declare" in out  # 末尾指针


def test_context_brief_omits_handoff_when_empty(tmp_path):
    """seed_context 为空 → 不出「已有工作(接手)」段;无匹配 action → 降级提示。"""
    ctx = {"prd_path": "", "seed_context": "", "_agent_actions": []}
    out = _build_context_brief(_story_row(tmp_path, context_json=ctx), "design")
    assert "已有工作(接手)" not in out
    assert "本阶段无结构化任务清单" in out


def test_context_brief_prd_unreadable_does_not_crash(tmp_path):
    """PRD 路径读不到 → 降级提示,不抛(镜像 todo 健壮性)。"""
    ctx = {"prd_path": str(tmp_path / "nope.md"), "_agent_actions": []}
    out = _build_context_brief(_story_row(tmp_path, context_json=ctx), "design")
    assert "读不到" in out


def test_context_brief_bad_context_json_degrades(tmp_path):
    """context_json 非法 → 当空 ctx,不抛,仍产出简报骨架。"""
    story = _story_row(tmp_path, context_json="{not valid json")
    out = _build_context_brief(story, "design")
    assert "任务简报" in out
    assert "本阶段无结构化任务清单" in out
