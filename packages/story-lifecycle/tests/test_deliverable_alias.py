"""deliverables gate 的 doc_type 别名解析(spec↔design)。

回归(2026-07-28):code agent 把设计文档落成 doc_type=design(design.md),
但 deliverables gate 只查 doc_type==spec → exists=False → 前端显示
"暂无产物,产物未生成无法确认"。文档实际存在。

根因:别名知识散落 4 处(artifact_check / planner._STAGE_DOC_KIND /
migrate_done / detector),deliverables 没接。修复:集中到
story_paths.DOC_TYPE_ALIASES,deliverables 用 resolve_doc_type_aliases 逐个候选试。
"""

from unittest.mock import patch

from story_lifecycle.sourcing.deliverables import check_deliverables


def _stub_story(monkeypatch, story_key="S1"):
    """story 行最小存根:check_deliverables 只读 context_json + workspace。"""
    monkeypatch.setattr(
        "story_lifecycle.sourcing.deliverables.db.get_story",
        lambda k: {
            "story_key": k,
            "workspace": "/ws",
            "context_json": "{}",
            "lifecycle_state": "开发",
        },
    )


def _patch_get_doc(monkeypatch, return_by_type):
    """get_story_doc(story_key, doc_type) → 按 doc_type 返回固定 doc 或 None。"""
    monkeypatch.setattr(
        "story_lifecycle.sourcing.deliverables.db.get_story_doc",
        lambda k, dt: return_by_type.get(dt),
    )


def _find(delivs, key):
    return next(d for d in delivs if d["key"] == key)


def test_spec_doc_exists_canonical(monkeypatch):
    """doc_type=spec 在 → 设计文档 exists(canonical 优先,不走别名)。"""
    _stub_story(monkeypatch)
    _patch_get_doc(monkeypatch, {"spec": {"confirmed_by": None}})
    spec = _find(check_deliverables("S1"), "spec")
    assert spec["exists"] is True


def test_design_alias_satisfies_spec_when_spec_absent(monkeypatch):
    """spec 不在、design 在 → 设计文档 exists(别名兜底,核心回归)。

    真实事件:tapd-...1066924 设计文档是 design.md/doc_type=design,spec 查不到 →
    显示"暂无产物"。修复后 design 也算 spec 成果物已落地。
    """
    _stub_story(monkeypatch)
    _patch_get_doc(monkeypatch, {"design": {"confirmed_by": None}})  # 只有 design,无 spec
    spec = _find(check_deliverables("S1"), "spec")
    assert spec["exists"] is True, "design 别名应满足 spec gate"
    assert spec["confirmed"] is False


def test_design_alias_confirmed_carries_through(monkeypatch):
    """design 行已确认 → spec 成果物 confirmed=True(别名行的确认状态透传)。"""
    _stub_story(monkeypatch)
    _patch_get_doc(monkeypatch, {"design": {"confirmed_by": "user"}})
    spec = _find(check_deliverables("S1"), "spec")
    assert spec["exists"] is True
    assert spec["confirmed"] is True


def test_spec_and_design_both_absent(monkeypatch):
    """spec 和 design 都不在 → 设计文档 not exists(无别名命中)。"""
    _stub_story(monkeypatch)
    _patch_get_doc(monkeypatch, {})
    spec = _find(check_deliverables("S1"), "spec")
    assert spec["exists"] is False


def test_spec_takes_precedence_over_design(monkeypatch):
    """spec 和 design 都在 → 用 canonical spec(确认状态取自 spec 行)。"""
    _stub_story(monkeypatch)
    _patch_get_doc(
        monkeypatch,
        {
            "spec": {"confirmed_by": None},  # spec 行未确认
            "design": {"confirmed_by": "user"},  # design 行已确认
        },
    )
    spec = _find(check_deliverables("S1"), "spec")
    assert spec["exists"] is True
    # canonical 优先,confirmed 取自 spec 行(未确认),不是 design 行。
    assert spec["confirmed"] is False


def test_test_report_no_alias_change(monkeypatch):
    """test_report 无别名 → 行为不变(只有 spec 在 doc / 都不在两态)。"""
    _stub_story(monkeypatch)
    _patch_get_doc(monkeypatch, {"test_report": {"confirmed_by": None}})
    tr = _find(check_deliverables("S1"), "test_report")
    assert tr["exists"] is True
