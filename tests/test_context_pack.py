"""Tests for context pack renderer (mixed-density, neutral)."""

from fastapi.testclient import TestClient
from story_lifecycle.orchestrator.context.pack import generate_pack
from story_lifecycle.orchestrator.api import app
from story_lifecycle.db import models as db


def _seed_story(key="S1", tmp_path=None):
    db.create_story(story_key=key, title="测试需求", workspace=str(tmp_path))


def test_pack_renders_branch_and_local_doc_path(isolated_story_home, tmp_path):
    _seed_story("S1", tmp_path)
    db.create_project(name="p1", repo_path=str(tmp_path))
    db.bind_story_project("S1", 1, branch="feature/S1")
    db.create_document("S1", kind="prd", ref="prd/S1.md", summary="需求摘要")
    content = generate_pack("S1")["content"]
    assert "feature/S1" in content  # 分支
    assert "prd/S1.md" in content  # 本地文档给路径
    assert "测试需求" in content


def test_pack_inlines_nacos_evidence(isolated_story_home, tmp_path):
    _seed_story("S2", tmp_path)
    db.create_change_item(
        "S2",
        kind="nacos",
        ref="hc-order.yaml",
        summary="改了超时",
        evidence_ref="timeout: 30s -> 60s",
    )
    content = generate_pack("S2")["content"]
    assert "timeout: 30s -> 60s" in content  # Nacos 正文内联
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


def test_pack_includes_parent_requirement(isolated_story_home, tmp_path):
    # parent 需求
    db.create_story(story_key="REQ-1", title="删除联系人", workspace=str(tmp_path))
    db.create_project(name="p", repo_path=str(tmp_path))
    db.bind_story_project("REQ-1", 1, branch="feature/x")
    db.create_document("REQ-1", kind="spec", ref="spec.md", summary="联系人删除")
    # bug with parent
    db.create_story(
        story_key="BUG-9",
        title="UID千分位",
        workspace=str(tmp_path),
        parent_key="REQ-1",
    )
    content = generate_pack("BUG-9")["content"]
    assert "关联需求" in content
    assert "删除联系人" in content  # parent title
    assert "feature/x" in content  # parent branch
    assert "spec.md" in content  # parent spec ref


def test_pack_skill_hint_when_param(isolated_story_home, tmp_path):
    _seed_story("S-skill", tmp_path)
    content = generate_pack("S-skill", skill="bug-fix")["content"]
    assert "建议调用 /bug-fix 处理" in content


def test_pack_no_skill_hint_by_default(isolated_story_home, tmp_path):
    _seed_story("S-noskill", tmp_path)
    content = generate_pack("S-noskill")["content"]
    assert "建议调用" not in content


def test_pack_flags_missing_refs(isolated_story_home, tmp_path):
    db.create_story(story_key="S-gap", title="t", workspace=str(tmp_path))
    content = generate_pack("S-gap")["content"]
    assert "⚠ 缺 spec" in content
    assert "⚠ 缺 branch" in content


def test_pack_no_gap_flags_when_complete(isolated_story_home, tmp_path):
    db.create_story(story_key="S-ok", title="t", workspace=str(tmp_path))
    db.create_project(name="p", repo_path=str(tmp_path))
    db.bind_story_project("S-ok", 1, branch="feature/x")
    db.create_document("S-ok", kind="spec", ref="spec.md")
    content = generate_pack("S-ok")["content"]
    assert "⚠ 缺" not in content


def test_pack_endpoint_returns_content(isolated_story_home, tmp_path):
    _seed_story("E1", tmp_path)
    client = TestClient(app)
    r = client.get("/api/story/E1/context/pack")
    assert r.status_code == 200
    body = r.json()
    assert "content" in body
    assert "E1" in body["content"]


def test_pack_endpoint_404_unknown_story(isolated_story_home):
    client = TestClient(app)
    r = client.get("/api/story/NOPE/context/pack")
    assert r.status_code == 404
