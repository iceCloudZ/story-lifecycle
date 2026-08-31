"""生产巡检 API 测试（docs/design-prod-patrol-integration.md Phase 2）。

覆盖验收场景 §6.1/§6.2/§6.3 的 API 层：items 登记与替换、轮次回写与历史、
train 包聚合 overview、story 列表徽标摘要 patrolSummary。
"""

import pytest
from fastapi.testclient import TestClient

from story_lifecycle.infra.db import models as db
from story_lifecycle.orchestrator.service.api import app


@pytest.fixture
def client(isolated_story_home):
    return TestClient(app)


def _seed_story(key: str, title: str = "patrol story", home=None):
    db.create_story(key, title, home)
    db.update_story(key, intake_state="ready")
    return key


ITEMS_BODY = {
    "items": [
        {
            "name": "ES错误面-hc-order",
            "type": "es_error_scan",
            "params": {"service": "hc-order", "keyword": "NumberFormatException", "window": "1h"},
            "baseline": "7d≈17条",
            "pass_criteria": "错误≤基线、绕过=0",
            "rollback_ref": "配置中心回滚开关",
            "enabled": True,
        },
        {
            "name": "灰度开关读数",
            "type": "nacos_read",
            "params": {"data_id": "hc-order.json", "key": "graySwitch"},
            "pass_criteria": "与发布单一致",
            "enabled": True,
        },
    ]
}


class TestPatrolItemsAPI:
    def test_put_and_get_items(self, client, isolated_story_home):
        key = _seed_story("tapd-patrol-1", home=str(isolated_story_home))
        resp = client.put(f"/api/story/{key}/patrol/items", json=ITEMS_BODY)
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert [it["seq"] for it in data["items"]] == [1, 2]
        first = data["items"][0]
        assert first["name"] == "ES错误面-hc-order"
        assert first["type"] == "es_error_scan"
        assert first["params"]["service"] == "hc-order"
        assert first["passCriteria"] == "错误≤基线、绕过=0"
        assert first["baseline"] == "7d≈17条"
        assert first["rollbackRef"] == "配置中心回滚开关"

        got = client.get(f"/api/story/{key}/patrol/items").json()
        assert [it["name"] for it in got["items"]] == ["ES错误面-hc-order", "灰度开关读数"]

    def test_put_replaces_full_set(self, client, isolated_story_home):
        """全量覆盖语义：第二次 PUT 只剩 1 项时，旧第 2 项必须消失、seq 重排。"""
        key = _seed_story("tapd-patrol-2", home=str(isolated_story_home))
        client.put(f"/api/story/{key}/patrol/items", json=ITEMS_BODY)
        resp = client.put(
            f"/api/story/{key}/patrol/items",
            json={"items": [{"name": "漏斗双口径", "type": "sql_count"}]},
        )
        assert resp.status_code == 200
        items = resp.json()["items"]
        assert len(items) == 1
        assert items[0]["name"] == "漏斗双口径"
        assert items[0]["seq"] == 1
        assert items[0]["type"] == "sql_count"

    def test_put_empty_items_clears_all(self, client, isolated_story_home):
        key = _seed_story("tapd-patrol-clear", home=str(isolated_story_home))
        client.put(f"/api/story/{key}/patrol/items", json=ITEMS_BODY)
        resp = client.put(f"/api/story/{key}/patrol/items", json={"items": []})
        assert resp.status_code == 200
        assert resp.json()["items"] == []

    def test_items_story_not_found(self, client, isolated_story_home):
        assert client.get("/api/story/tapd-nope/patrol/items").status_code == 404
        resp = client.put("/api/story/tapd-nope/patrol/items", json=ITEMS_BODY)
        assert resp.status_code == 404

    def test_put_rejects_blank_name(self, client, isolated_story_home):
        key = _seed_story("tapd-patrol-blank", home=str(isolated_story_home))
        resp = client.put(
            f"/api/story/{key}/patrol/items",
            json={"items": [{"name": "   "}]},
        )
        assert resp.status_code == 400


class TestPatrolRunsAPI:
    def test_post_run_and_history_with_rollup(self, client, isolated_story_home):
        key = _seed_story("tapd-patrol-run", home=str(isolated_story_home))
        client.put(f"/api/story/{key}/patrol/items", json=ITEMS_BODY)

        run_body = {
            "run_scope": "train:app-1.2.32",
            "executor": "ai:prod-patrol",
            "summary": "灰度首轮：错误面平稳",
            "items": [
                {"item_seq": 1, "result": "PASS", "observed": "12条(<基线17)", "evidence_ref": "patrol.md#r1"},
                {"item_seq": 2, "result": "WAIVED", "observed": "开关未开", "evidence_ref": "patrol.md#r1"},
            ],
        }
        resp = client.post(f"/api/story/{key}/patrol/runs", json=run_body)
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert data["result"] == "PASS"  # 无 FAIL → PASS（WAIVED 不翻红）
        run = data["run"]
        assert run["runScope"] == "train:app-1.2.32"
        assert run["items"][0]["name"] == "ES错误面-hc-order"  # 软引用补名

        # 第二轮打 FAIL → rollup FAIL，且历史新→旧
        client.post(
            f"/api/story/{key}/patrol/runs",
            json={
                "run_scope": "train:app-1.2.32",
                "executor": "ai:prod-patrol",
                "summary": "错误面突增",
                "items": [
                    {"item_seq": 1, "result": "FAIL", "observed": "83条(>基线17)", "evidence_ref": "patrol.md#r2"},
                ],
            },
        )
        history = client.get(f"/api/story/{key}/patrol/runs").json()
        assert len(history["runs"]) == 2
        assert history["runs"][0]["result"] == "FAIL"  # 最新在前
        assert history["runs"][0]["items"][0]["observed"] == "83条(>基线17)"
        assert history["runs"][1]["result"] == "PASS"

    def test_post_run_invalid_result_400(self, client, isolated_story_home):
        key = _seed_story("tapd-patrol-bad", home=str(isolated_story_home))
        resp = client.post(
            f"/api/story/{key}/patrol/runs",
            json={"items": [{"item_seq": 1, "result": "MAYBE"}]},
        )
        assert resp.status_code == 400
        assert "MAYBE" in resp.json()["detail"]

    def test_runs_story_not_found(self, client, isolated_story_home):
        assert client.get("/api/story/tapd-nope/patrol/runs").status_code == 404
        resp = client.post("/api/story/tapd-nope/patrol/runs", json={"items": []})
        assert resp.status_code == 404

    def test_run_records_event(self, client, isolated_story_home):
        key = _seed_story("tapd-patrol-evt", home=str(isolated_story_home))
        client.post(
            f"/api/story/{key}/patrol/runs",
            json={"summary": "ok", "items": [{"item_seq": 1, "result": "PASS"}]},
        )
        events = db.get_story_events(key)
        types = [e["event_type"] for e in events]
        assert "patrol_run_recorded" in types


class TestTrainPatrolOverview:
    def test_overview_aggregation(self, client, isolated_story_home):
        """验收 §6.1/§6.2：包跑一轮全 PASS 可见；改 FAIL 后聚合出 FAIL 徽标与明细。"""
        home = str(isolated_story_home)
        k1 = _seed_story("tapd-patrol-a", "需求A", home)
        k2 = _seed_story("tapd-patrol-b", "需求B", home)
        k3 = _seed_story("tapd-patrol-c", "需求C(未巡检)", home)
        db.update_story(k1, release_train="app-1.2.32")
        db.update_story(k2, release_train="app-1.2.32")
        db.update_story(k3, release_train="app-1.2.32")

        for k in (k1, k2):
            client.put(f"/api/story/{k}/patrol/items", json=ITEMS_BODY)

        # 首轮：A/B 全 PASS，C 从未巡检
        for k in (k1, k2):
            client.post(
                f"/api/story/{k}/patrol/runs",
                json={
                    "run_scope": "train:app-1.2.32",
                    "executor": "ai:prod-patrol",
                    "items": [
                        {"item_seq": 1, "result": "PASS", "observed": "ok"},
                        {"item_seq": 2, "result": "PASS", "observed": "ok"},
                    ],
                },
            )
        ov = client.get("/api/trains/app-1.2.32/patrol/overview").json()
        assert ov["total"] == 3
        assert ov["patrolled"] == 2
        assert ov["failed"] == 0
        assert ov["neverPatrolled"] == [k3]
        by_key = {s["storyKey"]: s for s in ov["stories"]}
        assert by_key[k1]["itemsCount"] == 2
        assert by_key[k1]["latestRun"]["result"] == "PASS"
        assert by_key[k3]["latestRun"] is None

        # 把 A 的一项改成 FAIL → overview 聚合可见 FAIL 明细
        client.post(
            f"/api/story/{k1}/patrol/runs",
            json={
                "run_scope": "train:app-1.2.32",
                "executor": "ai:prod-patrol",
                "items": [
                    {"item_seq": 1, "result": "FAIL", "observed": "83条", "evidence_ref": "patrol.md#r2"},
                ],
            },
        )
        ov2 = client.get("/api/trains/app-1.2.32/patrol/overview").json()
        assert ov2["failed"] == 1
        latest = {s["storyKey"]: s for s in ov2["stories"]}[k1]["latestRun"]
        assert latest["result"] == "FAIL"
        assert latest["failItems"][0]["name"] == "ES错误面-hc-order"
        assert latest["failItems"][0]["observed"] == "83条"

    def test_overview_empty_train(self, client, isolated_story_home):
        ov = client.get("/api/trains/be-2099-01-01/patrol/overview").json()
        assert ov == {
            "train": "be-2099-01-01",
            "total": 0,
            "patrolled": 0,
            "failed": 0,
            "neverPatrolled": [],
            "stories": [],
        }


class TestStoryListPatrolSummary:
    def test_list_carries_patrol_summary(self, client, isolated_story_home):
        """班车看板徽标数据源：GET /api/story 带 patrolSummary（null / PASS / FAIL）。"""
        home = str(isolated_story_home)
        k_pass = _seed_story("tapd-ps-pass", home=home)
        k_fail = _seed_story("tapd-ps-fail", home=home)
        k_none = _seed_story("tapd-ps-none", home=home)
        for k in (k_pass, k_fail):
            client.put(f"/api/story/{k}/patrol/items", json=ITEMS_BODY)
        client.post(
            f"/api/story/{k_pass}/patrol/runs",
            json={"items": [{"item_seq": 1, "result": "PASS"}]},
        )
        client.post(
            f"/api/story/{k_fail}/patrol/runs",
            json={"items": [{"item_seq": 1, "result": "FAIL"}]},
        )

        stories = {s["storyKey"]: s for s in client.get("/api/story").json()}
        assert stories[k_pass]["patrolSummary"] == {
            "itemsCount": 2,
            "latestRunAt": stories[k_pass]["patrolSummary"]["latestRunAt"],
            "latestResult": "PASS",
        }
        assert stories[k_fail]["patrolSummary"]["latestResult"] == "FAIL"
        assert stories[k_none]["patrolSummary"] is None
