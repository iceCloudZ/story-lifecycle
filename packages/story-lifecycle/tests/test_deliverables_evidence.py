"""deliverables 的 evidence 透传 + workspace child 扫描的绑定收紧。

回归(2026-07-30,tapd-...1067447 「上线交付」点击空 + code exists 选错仓):
1. delivery 交付物是 MR 驱动(查 story_delivery_artifact),不是文档驱动 ——
   story_doc 永远没有 delivery 行,前端无法走 /docs/delivery 取内容。修复:把命中的
   MR 行精简后作为 evidence 随 /deliverables 响应一起回,前端内联展示。
2. _pick_repo_and_branches 的 child 扫描在有绑定时只认绑定的 worktree_path,
   不再在共享 monorepo 根(D:\\hc-all)上盲扫到无关项目仓(hc-aiops)而误判 code exists。
"""

from unittest.mock import patch

from story_lifecycle.sourcing.deliverables import check_deliverables


def _stub_story(monkeypatch, story_key="S1"):
    """story 行最小存根:check_deliverables 只读 context_json。"""
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
    monkeypatch.setattr(
        "story_lifecycle.sourcing.deliverables.db.get_story_doc",
        lambda k, dt: return_by_type.get(dt),
    )


def _patch_delivery_artifacts(monkeypatch, artifacts):
    monkeypatch.setattr(
        "story_lifecycle.sourcing.deliverables.db.get_story_delivery_artifacts",
        lambda k: artifacts,
    )


def _find(delivs, key):
    return next(d for d in delivs if d["key"] == key)


# ---------------------------------------------------------------------------
# evidence 透传
# ---------------------------------------------------------------------------


def test_delivery_merged_mr_carries_evidence(monkeypatch):
    """merged MR → delivery exists=True 且 evidence 带该 MR(核心回归)。"""
    _stub_story(monkeypatch)
    _patch_get_doc(monkeypatch, {})
    _patch_delivery_artifacts(
        monkeypatch,
        [
            {
                "id": 42,
                "project_id": 5,
                "external_id": "37781",
                "url": "https://git/mr/37781",
                "source_branch": "feature/ice/credit_trigger_coupon",
                "target_branch": "test",
                "delivery_state": "merged",
                "review_state": "not_reviewed",
                "provider": "Skyladder",
                "evidence_ref": "build #529 triggered on test",
            }
        ],
    )
    delivery = _find(check_deliverables("S1"), "delivery")
    assert delivery["exists"] is True
    assert delivery["satisfied"] is False  # needs_confirm 且未确认
    assert len(delivery["evidence"]) == 1
    mr = delivery["evidence"][0]
    assert mr["external_id"] == "37781"
    assert mr["delivery_state"] == "merged"
    assert mr["source_branch"] == "feature/ice/credit_trigger_coupon"
    # 内部字段(id/project_id)不泄露给前端
    assert "id" not in mr
    assert "project_id" not in mr


def test_delivery_abandoned_mr_also_counts(monkeypatch):
    """abandoned MR 也算产物(与 merged 同档)。"""
    _stub_story(monkeypatch)
    _patch_get_doc(monkeypatch, {})
    _patch_delivery_artifacts(
        monkeypatch,
        [{"delivery_state": "abandoned", "external_id": "9"}],
    )
    delivery = _find(check_deliverables("S1"), "delivery")
    assert delivery["exists"] is True
    assert len(delivery["evidence"]) == 1


def test_delivery_no_landed_mr_no_evidence(monkeypatch):
    """只有 not_started/review_pending(未落地)的 MR → 不算产物,evidence 为空。"""
    _stub_story(monkeypatch)
    _patch_get_doc(monkeypatch, {})
    _patch_delivery_artifacts(
        monkeypatch,
        [{"delivery_state": "not_started"}, {"delivery_state": "review_pending"}],
    )
    delivery = _find(check_deliverables("S1"), "delivery")
    assert delivery["exists"] is False
    assert delivery["evidence"] == []


def test_delivery_no_artifacts_at_all(monkeypatch):
    """无任何 MR 行 → exists=False、evidence=[]。"""
    _stub_story(monkeypatch)
    _patch_get_doc(monkeypatch, {})
    _patch_delivery_artifacts(monkeypatch, [])
    delivery = _find(check_deliverables("S1"), "delivery")
    assert delivery["exists"] is False
    assert delivery["evidence"] == []


def test_non_delivery_items_have_empty_evidence(monkeypatch):
    """非 delivery 项(doc/code)evidence 字段稳定为空数组(前端类型不 panic)。"""
    _stub_story(monkeypatch)
    _patch_get_doc(monkeypatch, {"spec": {"confirmed_by": None}})
    _patch_delivery_artifacts(monkeypatch, [])
    delivs = check_deliverables("S1")
    spec = _find(delivs, "spec")
    assert spec["evidence"] == []
    code = _find(delivs, "code")
    assert code["evidence"] == []


def test_skipped_delivery_has_empty_evidence(monkeypatch):
    """跳过的 delivery:evidence 为空(字段稳定)。"""
    monkeypatch.setattr(
        "story_lifecycle.sourcing.deliverables.db.get_story",
        lambda k: {
            "story_key": k,
            "workspace": "/ws",
            "context_json": '{"_skipped_deliverables": ["delivery"]}',
            "lifecycle_state": "开发",
        },
    )
    _patch_get_doc(monkeypatch, {})
    _patch_delivery_artifacts(
        monkeypatch,
        [{"delivery_state": "merged", "external_id": "1"}],  # 即使有 MR,跳过也不展示
    )
    delivery = _find(check_deliverables("S1"), "delivery")
    assert delivery["skipped"] is True
    assert delivery["evidence"] == []
