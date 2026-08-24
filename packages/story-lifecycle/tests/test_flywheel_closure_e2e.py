"""飞轮闭环 M1 端到端回归(DESIGN-knowledge-flywheel-closure §5 M1 第 4 条)。

历史 bug(2026-08 实测):sourced 创建路径(bugs/sync → upsert_story_from_source)
不打 task_type,覆盖率 4/30;provider 无 task_type 即整条早退 → 87% story 执行时
飞轮知识注入为零。本文件走"创建 → 打标 → 注入"全链,断言 sourced story 也能
拿到知识注入。
"""

import json
import sys
from pathlib import Path

import pytest


def _ensure_knowledge_importable():
    ksrc = Path(__file__).resolve().parents[2] / "knowledge" / "src"
    if ksrc.is_dir() and str(ksrc) not in sys.path:
        sys.path.insert(0, str(ksrc))
    import knowledge  # noqa: F401

    return knowledge


@pytest.fixture(autouse=True)
def _isolated_kroot(tmp_path, monkeypatch):
    import story_lifecycle.infra.config as _cfg_mod

    monkeypatch.setattr(_cfg_mod, "get_config", lambda: {})
    monkeypatch.setenv(
        "STORY_KNOWLEDGE_ROOT", str(tmp_path / ".story" / "knowledge")
    )


def test_sourced_story_full_chain_gets_knowledge_injection(tmp_path):
    """sourced 创建(bug 标题含受控词汇)→ ensure_task_type → get_context 非空。"""
    try:
        _ensure_knowledge_importable()
    except ImportError:
        pytest.skip("knowledge package not available in this monorepo checkout")

    from story_lifecycle.infra.db import models as db
    from story_lifecycle.knowledge.context_providers.knowledge_provider import (
        KnowledgeContextProvider,
    )
    from story_lifecycle.orchestrator.service.story_service import ensure_task_type

    # 1. sourced 创建 —— sync_service 同款路径,不经 create_and_start_story
    story, _created = db.upsert_story_from_source(
        source_type="tapd",
        source_id="bug_42",
        title="授信额度计算错误",
        workspace=str(tmp_path),
        tapd_type="bug",
    )
    story_key = story["story_key"]

    # 2. sync 路径打标(关键词档)
    tt = ensure_task_type(story_key, use_llm=False)
    assert tt == "credit-limit"
    ctx = json.loads(db.get_story(story_key)["context_json"])
    assert ctx["task_type"] == "credit-limit"

    # 3. 知识根里放一个 linked 到本 story 的 by-story playbook
    kroot = tmp_path / ".story" / "knowledge"
    (kroot / "playbooks").mkdir(parents=True, exist_ok=True)
    (kroot / "INDEX.json").write_text(
        json.dumps(
            {
                "entries": [
                    {
                        "type": "playbook",
                        "id": "pb-story-42",
                        "title": "额度类 bug 先查风控回调",
                        "source": "dynamic",
                        "linked_story": story_key,
                        "tags": [],
                        "must_read": [],
                    }
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    # 4. 注入:全量路径(task_type 已打标)
    provider = KnowledgeContextProvider(
        config={"base_path": str(tmp_path / "no-out")}
    )
    out = provider.get_context(story_key, str(tmp_path), "design")
    assert out is not None, "打标后的 sourced story 必须有飞轮注入"
    assert "task_type=credit-limit" in out
    assert "额度类 bug 先查风控回调" in out  # by-story 精确命中

    # 5. 幂等:重同步再打标不覆盖
    assert ensure_task_type(story_key, title="营销活动") == "credit-limit"


def test_untagged_sourced_story_gets_degraded_injection(tmp_path):
    """B1 ③:标题无法分类的 sourced story → 降级注入(全局层),不再 return None。"""
    try:
        _ensure_knowledge_importable()
    except ImportError:
        pytest.skip("knowledge package not available in this monorepo checkout")

    from story_lifecycle.infra.db import models as db
    from story_lifecycle.knowledge.context_providers import knowledge_provider as kp

    db.upsert_story_from_source(
        source_type="tapd",
        source_id="bug_77",
        title="zzz 完全不匹配受控词汇的标题",
        workspace=str(tmp_path),
        tapd_type="bug",
    )
    kroot = tmp_path / ".story" / "knowledge"
    (kroot / "failures").mkdir(parents=True, exist_ok=True)
    (kroot / "failures" / "failure-knowledge.json").write_text(
        json.dumps(
            {
                "failures": [
                    {
                        "id": "failure:compile",
                        "title": "编译错误",
                        "display_category": "编译错误",
                        "frequency": {"hc-all": 9},
                        "detail": "cannot find symbol",
                        "mitigations": ["检查依赖"],
                    }
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    provider = kp.KnowledgeContextProvider(
        config={"base_path": str(tmp_path / "no-out")}
    )
    out = provider.get_context("tapd-bug_77", str(tmp_path), "design")
    assert out is not None, "降级注入必须非空(有全局 failure 知识时)"
    assert "降级为全局知识" in out
    assert "编译错误" in out
