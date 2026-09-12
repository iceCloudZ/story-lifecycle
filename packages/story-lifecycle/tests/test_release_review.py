"""发版窗口评估 API 测试(DESIGN-v1-work-agent WP-E §7)。

覆盖:
- train 登记(既有端点 ``PUT /api/story/{key}/release-train``,routers/lifecycle.py):
  写后 ``GET /api/story`` 可见 ``releaseTrain``;未知 story 404;空串清空(现契约)
- 聚合 ``GET /api/trains/{train}/release-review``:per-story 回滚风险 high/low/medium、
  branch/base_commit、MR 未合清单、patrol 结论(含 train 轮借用)、rollup 计数
- 未知 train → 404;空白 train → 400
- DDL 扫描失败(workspace 不可读)→ ddl null,绝不 500
"""

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from story_lifecycle.infra.db import models as db
from story_lifecycle.infra.story_paths import story_evidence_dir
from story_lifecycle.orchestrator.service.api import app

TRAIN = "app-1.2.33"


@pytest.fixture
def client(isolated_story_home):
    return TestClient(app)


def _seed_story(key, title, workspace, train=TRAIN, **extra):
    db.create_story(key, title, str(workspace))
    db.update_story(key, release_train=train, intake_state="ready", **extra)
    return key


def _make_ws(home, name):
    """带 .agents 标记的 tmp workspace。

    story_evidence_root 会向上找 .agents/AGENTS.md 决定 story/ 证据根 —— 不打
    标记时会一路走到真实用户目录(真实发生过:测试把 ddl.sql 写进
    ~/story/)。打了标记,证据根稳定停在 tmp 内。
    """
    ws = home / name
    (ws / ".agents").mkdir(parents=True, exist_ok=True)
    return ws


class TestTrainRegistration:
    """train 登记:复用既有 PUT /release-train(WP-E 验收「写后 GET /api/story 带 train」)。"""

    def test_write_then_visible_in_story_list(self, client, isolated_story_home):
        key = _seed_story("RR-REG-1", "班车登记", isolated_story_home, train=None)
        resp = client.put(f"/api/story/{key}/release-train", json={"train": TRAIN})
        assert resp.status_code == 200
        assert resp.json()["releaseTrain"] == TRAIN

        stories = {s["storyKey"]: s for s in client.get("/api/story").json()}
        assert stories[key]["releaseTrain"] == TRAIN

    def test_unknown_story_404(self, client, isolated_story_home):
        resp = client.put("/api/story/rr-nope/release-train", json={"train": TRAIN})
        assert resp.status_code == 404

    def test_empty_string_clears_train(self, client, isolated_story_home):
        """现契约:空串归一为 None(回待分配区),不报 400 —— 与班车看板拖拽语义一致。"""
        key = _seed_story("RR-REG-2", "班车清空", isolated_story_home)
        assert client.put(f"/api/story/{key}/release-train", json={"train": TRAIN}).status_code == 200
        resp = client.put(f"/api/story/{key}/release-train", json={"train": ""})
        assert resp.status_code == 200
        assert resp.json()["releaseTrain"] is None
        assert db.get_story(key)["release_train"] is None


class TestReleaseReviewAggregation:
    def test_two_stories_high_and_low(self, client, isolated_story_home):
        """验收线:一带 DDL 一纯代码同 train → per-story + rollup,风险 high/low 正确。"""
        home = isolated_story_home
        ws = _make_ws(home, "ws")

        k1 = _seed_story("tapd-111", "带DDL的需求", ws, lifecycle_state="测试")
        ddl_dir = story_evidence_dir(str(ws), k1, "带DDL的需求")
        ddl_dir.mkdir(parents=True, exist_ok=True)
        (ddl_dir / "ddl.sql").write_text("ALTER TABLE t ADD COLUMN c INT;", encoding="utf-8")
        proj = db.create_project("proj-a", str(ws / "proj-a"), "main")
        db.bind_story_project(k1, proj["id"], branch="feat/111", base_commit="abc123")
        db.create_delivery_artifact(
            k1,
            kind="mr",
            provider="gitlab",
            external_id="!101",
            url="https://git.example/group/!101",
            source_branch="feat/111",
            target_branch="main",
            delivery_state="open",
            evidence_ref="mr-101.json",
        )
        db.create_patrol_run(
            k1,
            f"train:{TRAIN}",
            "ai:prod-patrol",
            "错误面突增",
            items=[
                {"item_seq": 1, "result": "FAIL", "observed": "83条", "evidence_ref": "patrol.md#r2"}
            ],
        )

        k2 = _seed_story("tapd-222", "纯代码需求", ws)

        resp = client.get(f"/api/trains/{TRAIN}/release-review")
        assert resp.status_code == 200
        data = resp.json()
        assert data["train"] == TRAIN
        stories = {s["story_key"]: s for s in data["stories"]}
        assert set(stories) == {k1, k2}
        s1, s2 = stories[k1], stories[k2]

        # 带 DDL story:high + 硬证据(ddl 文件,不待 skill 研判)
        assert s1["rollback_risk"]["level"] == "high"
        assert s1["rollback_risk"]["pending_skill_review"] is False
        assert s1["ddl"]["size"] > 0
        assert s1["ddl"]["path"].endswith("ddl.sql")
        # 分支/base_commit(story_project)
        assert s1["projects"][0]["branch"] == "feat/111"
        assert s1["projects"][0]["base_commit"] == "abc123"
        # delivery artifacts(MR 证据)
        assert s1["mrs"][0]["source_branch"] == "feat/111"
        assert s1["mrs"][0]["target_branch"] == "main"
        assert s1["mrs"][0]["evidence_ref"] == "mr-101.json"
        # 本 story 自己的 FAIL 巡检轮
        assert s1["patrol"]["result"] == "FAIL"
        assert s1["patrol"]["failed_count"] == 1
        assert s1["patrol"]["inherited_from_train"] is False
        # 四件套 gate 状态复用 /deliverables 的条目结构
        assert {g["key"] for g in s1["gates"]} == {"prd", "spec", "code", "test_report", "delivery"}
        assert s1["unmet_deliverables"]  # 全新 story 必有未满足成果物

        # 纯代码 story:low、无 DDL;没自己跑过巡检 → 借用 train 轮(FAIL)
        assert s2["rollback_risk"]["level"] == "low"
        assert s2["ddl"] is None
        assert s2["mrs"] == []
        assert s2["patrol"]["result"] == "FAIL"
        assert s2["patrol"]["inherited_from_train"] is True

        # rollup
        rollup = data["rollup"]
        assert rollup["total"] == 2
        assert rollup["risk_counts"] == {"high": 1, "medium": 0, "low": 1}
        assert [m["external_id"] for m in rollup["mrs_not_merged"]] == ["!101"]
        assert all(m["story_key"] == k1 for m in rollup["mrs_not_merged"])
        assert rollup["latest_patrol"]["result"] == "FAIL"
        assert rollup["latest_patrol"]["failed_count"] == 1
        assert {g["story_key"] for g in rollup["unmet_gates"]} == {k1, k2}

    def test_medium_risk_keyword_match_pending_review(self, client, isolated_story_home):
        """impact 登记命中风险关键词 → medium + pending_skill_review=true(真判断归 skill)。"""
        ws = _make_ws(isolated_story_home, "ws-med")
        k = _seed_story("tapd-333", "nacos配置需求", ws)
        db.create_document(
            k,
            "impact",
            ref="story/333-nacos/impact.md",
            summary="涉及 nacos 配置变更与 api bump",
        )
        data = client.get(f"/api/trains/{TRAIN}/release-review").json()
        s = data["stories"][0]
        assert s["rollback_risk"]["level"] == "medium"
        assert s["rollback_risk"]["pending_skill_review"] is True
        assert [d["kind"] for d in s["docs"]] == ["impact"]
        assert data["rollup"]["risk_counts"] == {"high": 0, "medium": 1, "low": 0}

    def test_registered_ddl_doc_is_high_without_file(self, client, isolated_story_home):
        """已登记 kind=ddl 文档(无 ddl.sql 文件)同样判 high。"""
        ws = _make_ws(isolated_story_home, "ws-ddldoc")
        k = _seed_story("tapd-444", "登记DDL文档", ws)
        db.create_document(k, "ddl", ref="db/migrations/001_add_col.sql", summary="订单表加列")
        data = client.get(f"/api/trains/{TRAIN}/release-review").json()
        s = data["stories"][0]
        assert s["ddl"] is None  # 文件没落地
        assert s["rollback_risk"]["level"] == "high"

    def test_unknown_train_404(self, client, isolated_story_home):
        resp = client.get("/api/trains/be-2099-01-01/release-review")
        assert resp.status_code == 404
        assert "be-2099-01-01" in resp.json()["detail"]

    def test_blank_train_400(self, isolated_story_home):
        from story_lifecycle.orchestrator.service.routers.release_review import (
            train_release_review,
        )

        with pytest.raises(HTTPException) as ei:
            train_release_review("   ")
        assert ei.value.status_code == 400

    def test_ddl_scan_failure_returns_null_not_500(self, client, isolated_story_home, monkeypatch):
        """workspace 不可读(路径解析抛异常)→ ddl null,端点照常 200。"""
        ws = _make_ws(isolated_story_home, "ws-broken")
        _seed_story("tapd-555", "坏路径需求", ws)

        def _boom(*args, **kwargs):
            raise RuntimeError("workspace unreadable")

        monkeypatch.setattr(
            "story_lifecycle.infra.story_paths.story_evidence_dir", _boom
        )
        resp = client.get(f"/api/trains/{TRAIN}/release-review")
        assert resp.status_code == 200
        assert resp.json()["stories"][0]["ddl"] is None

    def test_deleted_story_excluded_from_train(self, client, isolated_story_home):
        """软删 story 不进聚合(与 patrol overview 的 deleted_at IS NULL 同口径)。"""
        ws = _make_ws(isolated_story_home, "ws-del")
        k = _seed_story("tapd-666", "被删需求", ws)
        db.soft_delete_story(k)
        resp = client.get(f"/api/trains/{TRAIN}/release-review")
        assert resp.status_code == 404  # train 下只剩软删 story → 视为空车
