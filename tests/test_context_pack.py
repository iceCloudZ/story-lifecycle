"""Tests for context pack renderer (mixed-density, neutral)."""
import pytest
from story_lifecycle.orchestrator.context.pack import generate_pack
from story_lifecycle.db import models as db


def _seed_story(key="S1", tmp_path=None):
    db.create_story(story_key=key, title="测试需求", workspace=str(tmp_path))


def test_pack_renders_branch_and_local_doc_path(isolated_story_home, tmp_path):
    _seed_story("S1", tmp_path)
    db.create_project(name="p1", repo_path=str(tmp_path))
    db.bind_story_project("S1", 1, branch="feature/S1")
    db.create_document("S1", kind="prd", ref="prd/S1.md", summary="需求摘要")
    content = generate_pack("S1")["content"]
    assert "feature/S1" in content      # 分支
    assert "prd/S1.md" in content       # 本地文档给路径
    assert "测试需求" in content


def test_pack_inlines_nacos_evidence(isolated_story_home, tmp_path):
    _seed_story("S2", tmp_path)
    db.create_change_item(
        "S2", kind="nacos", ref="hc-order.yaml",
        summary="改了超时", evidence_ref="timeout: 30s -> 60s",
    )
    content = generate_pack("S2")["content"]
    assert "timeout: 30s -> 60s" in content   # Nacos 正文内联
    assert "改了超时" in content
    assert "## Nacos" in content


def test_pack_ddl_uses_path_not_inlining(isolated_story_home, tmp_path):
    _seed_story("S3", tmp_path)
    db.create_change_item("S3", kind="ddl", ref="sql/V1__add_col.sql", summary="加列")
    content = generate_pack("S3")["content"]
    assert "sql/V1__add_col.sql" in content
    assert "## DDL" in content


def test_pack_is_neutral_no_instruction(isolated_story_home, tmp_path):
    _seed_story("S4", tmp_path)
    content = generate_pack("S4")["content"]
    assert "请实现" not in content
    assert "请修复" not in content
    assert "请按" not in content


def test_pack_returns_revision(isolated_story_home, tmp_path):
    _seed_story("S5", tmp_path)
    result = generate_pack("S5")
    assert result["revision"] == 0
    assert result["story_key"] == "S5"