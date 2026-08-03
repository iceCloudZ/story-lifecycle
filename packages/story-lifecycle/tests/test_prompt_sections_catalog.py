"""设计 10 改动 2.1:scenario_catalog 注入段 + 容错。"""

from __future__ import annotations

import json

import pytest

from story_lifecycle.orchestrator.engine.prompt_sections import (
    build_scenario_catalog_section,
)


def _write_scenario(knowledge_dir, rel_path, content):
    path = knowledge_dir / rel_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


@pytest.fixture
def knowledge_root(tmp_path, monkeypatch):
    """造一个带 scenario 的 knowledge 目录,并把 _KNOWLEDGE_ROOT 指过去。"""
    root = tmp_path / "knowledge"
    _write_scenario(
        root,
        "scenarios/core-borrow/borrow.md",
        "---\n"
        "id: scenario:borrow-flow\n"
        "title: 借款放款流程\n"
        "participating_services: [hc-order, hc-user]\n"
        "---\n"
        "# 借款放款流程\n",
    )
    _write_scenario(
        root,
        "scenarios/core-repay/repay.md",
        "---\nid: scenario:repay-flow\ntitle: 还款流程\n---\n# 还款流程\n",
    )
    _write_scenario(
        root,
        "scenarios/core-repay/repay.md.json",
        json.dumps(
            {
                "apis": ["POST /api/loan/repay", "GET /api/loan/status"],
                "test_ref": "journeys/test_repay.py",
            },
            ensure_ascii=False,
        ),
    )
    monkeypatch.setattr(
        "story_lifecycle.knowledge.context_providers.knowledge_provider._KNOWLEDGE_ROOT",
        root,
    )
    return root


def test_catalog_lists_scenarios_with_details(knowledge_root):
    section = build_scenario_catalog_section("S-1", "/tmp", "planning")
    assert "候选测试场景" in section
    assert "`scenario:borrow-flow` — 借款放款流程" in section
    assert "服务: hc-order, hc-user" in section
    # sidecar 的 apis / test_ref 也被渲染(设计 10 改动 4 的填充落地)
    assert "API: POST /api/loan/repay" in section
    assert "journey: journeys/test_repay.py" in section


def test_catalog_empty_when_no_scenarios(tmp_path, monkeypatch):
    empty_root = tmp_path / "empty-knowledge"
    monkeypatch.setattr(
        "story_lifecycle.knowledge.context_providers.knowledge_provider._KNOWLEDGE_ROOT",
        empty_root,
    )
    assert build_scenario_catalog_section("S-1", "/tmp", "planning") == ""


def test_catalog_never_raises(monkeypatch):
    """knowledge 包不可用/目录不存在 → 空串,不阻断规划。"""
    monkeypatch.setattr(
        "story_lifecycle.knowledge.context_providers.knowledge_provider._KNOWLEDGE_ROOT",
        None,  # 导致 KnowledgeIndex(None) 抛异常 → 走容错分支
    )
    assert build_scenario_catalog_section("S-1", "/tmp", "planning") == ""
