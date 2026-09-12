"""WP-D 回归 — 巡检 FAIL → ``patrol_failed`` 管家事件(interrupt 档)。

- POST FAIL 轮次(含带 evidence_ref 的失败项)→ 200 且 outbox 恰好一行
  pending ``patrol_failed``,payload 含 run_id + 失败项 evidence_ref
- POST PASS 轮次 → 不出 patrol_failed 行
- 同一 body 重放:``post_patrol_run`` 无 run_id 幂等(请求体不带 run_id,
  ``create_patrol_run`` 每次恒 INSERT 新行)——「重放同一 run」在 API 层不可
  表达,每次 POST 都是新建 run。回归守卫 = 每条告警的 run_id 与本次响应
  一一对应,不存在同一 run_id 双发。
"""

import json

import pytest
from fastapi.testclient import TestClient

from story_lifecycle.infra.db import models as db
from story_lifecycle.infra.notification import router as notif_router
from story_lifecycle.orchestrator.service.api import app


@pytest.fixture
def client(isolated_story_home):
    return TestClient(app)


@pytest.fixture
def interrupt_config(monkeypatch):
    """无 routes 覆盖 + 显式关闭免打扰的干净配置:patrol_failed 稳定走
    代码内默认 interrupt 档,不受真实 config.yaml / 跑测时刻影响。"""
    monkeypatch.setattr(
        "story_lifecycle.infra.config.get_config",
        lambda: {"notification": {"quiet_hours": None}},
    )


def _seed_story(key: str, home) -> str:
    db.create_story(key, "patrol alert story", str(home))
    db.update_story(key, intake_state="ready")
    return key


def _patrol_failed_rows(story_key: str) -> list[dict]:
    return [
        r
        for r in db.list_notifications()
        if r["event_type"] == "patrol_failed" and r["story_key"] == story_key
    ]


def test_patrol_failed_default_route_is_interrupt():
    """路由表:patrol_failed ∈ DEFAULT_ROUTES 且为 interrupt 档(微信+桌面)。"""
    assert notif_router.DEFAULT_ROUTES.get("patrol_failed") == notif_router.TIER_INTERRUPT
    actions = notif_router.route(
        "patrol_failed", config={"notification": {"quiet_hours": None}}
    )
    assert [a.channel for a in actions] == ["wechat", "desktop"]
    assert all(a.tier == notif_router.TIER_INTERRUPT for a in actions)


class TestPatrolFailAlert:
    def test_fail_run_emits_one_pending_alert_with_evidence(
        self, client, isolated_story_home, interrupt_config
    ):
        """FAIL 轮次 → 恰好一行 pending patrol_failed,payload 带 run_id+evidence。"""
        key = _seed_story("tapd-alert-fail", isolated_story_home)
        client.put(
            f"/api/story/{key}/patrol/items",
            json={"items": [{"name": "ES错误面-hc-order", "type": "es_error_scan"}]},
        )

        resp = client.post(
            f"/api/story/{key}/patrol/runs",
            json={
                "run_scope": "train:app-1.2.32",
                "executor": "ai:prod-patrol",
                "summary": "错误面突增",
                "items": [
                    {
                        "item_seq": 1,
                        "result": "FAIL",
                        "observed": "83条(>基线17)",
                        "evidence_ref": "patrol.md#r2",
                    },
                    {"item_seq": 2, "result": "PASS", "observed": "ok"},
                ],
            },
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["result"] == "FAIL"
        run_id = body["runId"]

        rows = _patrol_failed_rows(key)
        assert len(rows) == 1
        row = rows[0]
        assert row["status"] == "pending"  # 只写 outbox 一行,投递归异步线程
        assert row["tier"] == "interrupt"
        assert "巡检" in row["title"] and "FAIL" in row["title"]
        payload = json.loads(row["payload_json"])
        assert payload["run_id"] == run_id
        assert payload["story_key"] == key
        assert payload["train"] == "app-1.2.32"
        assert payload["failed_items"] == [
            {"name": "ES错误面-hc-order", "result": "FAIL", "evidence_ref": "patrol.md#r2"}
        ]

    def test_pass_run_emits_no_alert(self, client, isolated_story_home, interrupt_config):
        """PASS 轮次 → 不出 patrol_failed 行(SKIP/WAIVED 不翻红,同理不发)。"""
        key = _seed_story("tapd-alert-pass", isolated_story_home)
        resp = client.post(
            f"/api/story/{key}/patrol/runs",
            json={
                "summary": "灰度首轮平稳",
                "items": [
                    {"item_seq": 1, "result": "PASS"},
                    {"item_seq": 2, "result": "WAIVED", "evidence_ref": "patrol.md#r1"},
                ],
            },
        )
        assert resp.status_code == 200
        assert resp.json()["result"] == "PASS"
        assert _patrol_failed_rows(key) == []

    def test_same_body_repost_is_new_run_alert_binds_to_its_own_run_id(
        self, client, isolated_story_home, interrupt_config
    ):
        """API 无 run_id 幂等重放:同 body 二次 POST = 新建 run(新 run_id),
        各出一条告警且 run_id 与各自响应一一对应 —— 不存在同一 run_id 双发。"""
        key = _seed_story("tapd-alert-replay", isolated_story_home)
        run_body = {
            "run_scope": "train:app-1.2.33",
            "executor": "ai:prod-patrol",
            "summary": "同一份失败结果",
            "items": [
                {"item_seq": 1, "result": "FAIL", "evidence_ref": "patrol.md#r9"}
            ],
        }
        first = client.post(f"/api/story/{key}/patrol/runs", json=run_body).json()
        second = client.post(f"/api/story/{key}/patrol/runs", json=run_body).json()
        assert first["runId"] != second["runId"]  # 每次 POST 都是新 run

        rows = _patrol_failed_rows(key)
        assert len(rows) == 2
        payload_run_ids = {
            json.loads(r["payload_json"])["run_id"] for r in rows
        }
        assert payload_run_ids == {first["runId"], second["runId"]}
