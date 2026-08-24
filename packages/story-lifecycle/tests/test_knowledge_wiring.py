"""ISS-009 9a contract test: KnowledgeContextProvider surfaces playbook/scenario/failure
knowledge via the `knowledge` contract package (KnowledgeIndex.retrieve), turning the
④ knowledge layer into a runtime contract. Verifies the wiring is live (not aspirational)
when the knowledge package is present, and degrades gracefully when absent."""

import json
import sys
from pathlib import Path

import pytest


def _ensure_knowledge_importable():
    """Make the monorepo knowledge package importable (packages/knowledge/src).

    story-lifecycle does not declare story-knowledge as a runtime dep yet, so in
    the standalone install path `from knowledge import KnowledgeIndex` ImportErrors
    and the provider degrades gracefully. In the monorepo test env we put it on
    sys.path to exercise the real integration.
    """
    ksrc = Path(__file__).resolve().parents[2] / "knowledge" / "src"
    if ksrc.is_dir() and str(ksrc) not in sys.path:
        sys.path.insert(0, str(ksrc))
    import knowledge  # noqa: F401
    return knowledge


def test_get_context_surfaces_knowledge_index_playbook(monkeypatch, tmp_path):
    """A playbook linked to the story is surfaced via KnowledgeIndex.retrieve()."""
    try:
        _ensure_knowledge_importable()
    except ImportError:
        pytest.skip("knowledge package not available in this monorepo checkout")

    from story_lifecycle.knowledge.context_providers import knowledge_provider as kp

    # knowledge dir with one playbook linked to the story
    kdir = tmp_path / "knowledge"
    kdir.mkdir()
    (kdir / "INDEX.json").write_text(
        json.dumps(
            {
                "entries": [
                    {
                        "type": "playbook",
                        "id": "pb-amount-validate",
                        "title": "Always validate amounts are positive",
                        "source": "miner",
                        "linked_story": "TEST-001",
                        "tags": ["amount", "validation"],
                        "must_read": ["confirm amount > 0 at boundary"],
                        "top_files": [],
                        "common_commands": [],
                        "common_failures": [],
                        "linked_scenarios": [],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(kp, "resolve_knowledge_root", lambda workspace: kdir)
    # No raw miner artifacts under base_path -> output relies on bootstrap + knowledge
    provider = kp.KnowledgeContextProvider(
        config={"base_path": str(tmp_path / "no-artifacts")}
    )
    monkeypatch.setattr(provider, "_task_type_for", lambda key: "fund-flow")

    out = provider.get_context("TEST-001", str(tmp_path), "design")

    assert out is not None
    # The playbook title must appear — proving KnowledgeIndex.retrieve() wired through
    assert "Always validate amounts are positive" in out
    assert "必读" in out  # must_read rendered


def test_get_context_degrades_gracefully_without_knowledge_pkg(monkeypatch, tmp_path):
    """If `knowledge` can't be imported, get_context still returns the rest (no crash)."""
    import builtins

    from story_lifecycle.knowledge.context_providers import knowledge_provider as kp

    real_import = builtins.__import__

    def _block_knowledge(name, *args, **kwargs):
        if name == "knowledge":
            raise ImportError("blocked for test")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _block_knowledge)
    provider = kp.KnowledgeContextProvider(
        config={"base_path": str(tmp_path / "no-artifacts")}
    )
    monkeypatch.setattr(provider, "_task_type_for", lambda key: "fund-flow")

    out = provider.get_context("TEST-001", str(tmp_path), "design")
    # No crash; returns the bootstrap/structure output (knowledge section silently absent)
    assert out is not None
    assert "飞轮知识上下文" in out


def test_degraded_injection_when_task_type_missing(monkeypatch, tmp_path):
    """B1 ③ 回归:task_type 缺失不再 return None —— 降级注入全局层。

    历史 bug:get_context 第一步 task_type 为空即早退,实测 87% story 零注入。
    降级层 = 全局高频失败 + 知识库检索(标题 query)+ wiki 摘要。
    """
    try:
        _ensure_knowledge_importable()
    except ImportError:
        pytest.skip("knowledge package not available in this monorepo checkout")

    from story_lifecycle.infra.db import models as db
    from story_lifecycle.knowledge.context_providers import knowledge_provider as kp

    kdir = tmp_path / "knowledge"
    (kdir / "failures").mkdir(parents=True)
    (kdir / "failures" / "failure-knowledge.json").write_text(
        json.dumps(
            {
                "failures": [
                    {
                        "id": "failure:compile",
                        "title": "编译错误",
                        "display_category": "编译错误",
                        "frequency": {"hc-all": 12},
                        "detail": "cannot find symbol",
                        "mitigations": ["检查依赖版本", "重新编译"],
                    }
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(kp, "resolve_knowledge_root", lambda workspace: kdir)

    # 无 task_type 的存量 story(三级解析全部落空)
    db.create_story("BUG-1", "某个完全无法分类的标题", "D:/ws", current_stage="design")
    provider = kp.KnowledgeContextProvider(
        config={"base_path": str(tmp_path / "no-artifacts")}
    )
    monkeypatch.setattr(provider, "_task_type_from_artifact", lambda key: None)
    monkeypatch.setattr(provider, "_lazy_keyword_backfill", lambda key: None)

    out = provider.get_context("BUG-1", str(tmp_path), "design")

    assert out is not None
    assert "降级为全局知识" in out
    assert "编译错误" in out
    assert "12" in out  # frequency 渲染


def test_lazy_keyword_backfill_heals_db(monkeypatch, tmp_path):
    """存量 story 懒自愈:DB/artifact 都无 task_type 时关键词命中即回写 DB。"""
    from story_lifecycle.infra.db import models as db
    from story_lifecycle.knowledge.context_providers import knowledge_provider as kp

    db.create_story("OLD-1", "还款页面无法提交", "D:/ws", current_stage="design")
    provider = kp.KnowledgeContextProvider(
        config={"base_path": str(tmp_path / "no-artifacts")}
    )
    monkeypatch.setattr(provider, "_task_type_from_artifact", lambda key: None)

    tt = provider._task_type_for("OLD-1")
    assert tt == "fund-flow"
    # 自愈:下次直接命中 DB 层
    story = db.get_story("OLD-1")
    assert json.loads(story["context_json"]).get("task_type") == "fund-flow"
