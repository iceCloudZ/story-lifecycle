"""WP-F §8.2 读侧 — GET /api/knowledge/search 检索面测试。

红线(设计 8.2):
- 命中:results 带 source_refs(knowledge 包 KnowledgeIndex.retrieve 薄包装);
- 未命中:200 + 空结果(无 warning);
- 降级:根不可用等任何失败 → 200 + {"results": [], "warning": ...},**绝不 500**。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_PKG_DIR = Path(__file__).resolve().parents[1]


def _ensure_knowledge_importable():
    ksrc = REPO_PKG_DIR.parent / "knowledge" / "src"
    if ksrc.is_dir() and str(ksrc) not in sys.path:
        sys.path.insert(0, str(ksrc))
    import knowledge  # noqa: F401

    return knowledge


RUN_MD = """# RUN — tapd-1144381896001069424 (2026-09-12)

## 本轮新坑（候选回写）

| 坑 | 规则 |
|---|---|
| 联系人落表坑 | 三方返回要落表,别只落日志 |
"""


@pytest.fixture(autouse=True)
def _isolated_kroot(tmp_path, monkeypatch):
    """知识根指向 tmp(config 置空防真机 config.yaml 的 knowledge_root 抢先)。"""
    import story_lifecycle.infra.config as _cfg_mod

    monkeypatch.setattr(_cfg_mod, "get_config", lambda: {})
    monkeypatch.setenv("STORY_KNOWLEDGE_ROOT", str(tmp_path / "kroot"))
    try:
        _ensure_knowledge_importable()
    except ImportError:
        pytest.skip("knowledge package not available in this monorepo checkout")


@pytest.fixture
def client():
    from fastapi.testclient import TestClient

    from story_lifecycle.orchestrator.service.api import app

    return TestClient(app)


@pytest.fixture
def run_md_path(tmp_path) -> Path:
    md = tmp_path / "RUN-tapd-1144381896001069424-20260912.md"
    md.write_text(RUN_MD, encoding="utf-8")
    return md


@pytest.fixture
def populated_kroot(tmp_path, run_md_path) -> Path:
    """经 8.1 写侧灌一条新坑(写读同根,顺带验证飞轮闭环)。"""
    from story_lifecycle.knowledge.knowledge_store.run_pitfalls import (
        import_run_pitfalls,
    )

    summary = import_run_pitfalls(run_md_path, root=tmp_path / "kroot")
    assert summary["imported"] == 1
    return tmp_path / "kroot"


# ---- 命中 ----


def test_hit_returns_results_with_source_refs(client, populated_kroot, run_md_path):
    resp = client.get("/api/knowledge/search", params={"q": "联系人落表"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["count"] == 1
    item = body["results"][0]
    assert item["title"] == "联系人落表坑"
    assert item["category"] == "run-pitfall"
    assert item["tags"] == ["tapd-1144381896001069424", "run-pitfall"]
    # source_refs 指向 RUN md 绝对路径(设计 8.2:响应带 source_refs)
    assert item["source_refs"] == [str(run_md_path.resolve())]


def test_story_key_resolves_via_story_workspace(client, populated_kroot, tmp_path):
    """带 story_key → 用该 story 的 workspace 解析知识根(读侧契约)。"""
    from story_lifecycle.infra.db import models as db

    story, _ = db.upsert_story_from_source(
        source_type="tapd",
        source_id="bug_ksearch_1",
        title="联系人落表",
        workspace=str(tmp_path),
    )
    resp = client.get(
        "/api/knowledge/search",
        params={"q": "联系人落表", "story_key": story["story_key"]},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["count"] == 1
    assert body["story_key"] == story["story_key"]


# ---- 未命中 ----


def test_miss_returns_empty_results_without_warning(client, populated_kroot):
    resp = client.get("/api/knowledge/search", params={"q": "完全无关词zzz"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["results"] == []
    assert body["count"] == 0
    assert "warning" not in body


def test_top_k_and_stage_params_accepted(client, populated_kroot):
    resp = client.get(
        "/api/knowledge/search",
        params={"q": "联系人落表", "stage": "build", "top_k": 3},
    )
    assert resp.status_code == 200
    assert resp.json()["stage"] == "build"


# ---- 降级(设计:失败 → 200 空结果 + warning,不 500) ----


def test_degraded_bad_root_returns_200_with_warning(client, tmp_path, monkeypatch):
    """知识根指向一个文件 → 检索炸 → 200 + warning 字段,绝不 500。"""
    bad_root = tmp_path / "kroot-is-a-file"
    bad_root.write_text("not a dir", encoding="utf-8")
    monkeypatch.setenv("STORY_KNOWLEDGE_ROOT", str(bad_root))

    resp = client.get("/api/knowledge/search", params={"q": "联系人落表"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["results"] == []
    assert "知识检索不可用" in body["warning"]


def test_degraded_index_builder_crash_returns_warning(client, populated_kroot, monkeypatch):
    """索引构造内部炸(非路径问题)→ 同款 200 降级。"""
    import knowledge as knowledge_pkg

    def _boom(*a, **k):
        raise RuntimeError("index exploded")

    monkeypatch.setattr(knowledge_pkg, "KnowledgeIndex", _boom)
    resp = client.get("/api/knowledge/search", params={"q": "联系人落表"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["results"] == []
    assert "index exploded" in body["warning"]
