"""WP-B 洞③回归 — /lifecycle/advance 停门管家事件 + 结构化 500。

- gate 未满足 → 409 且 notification_outbox 落 gate_waiting 行(此前静默 409,
  管家/微信侧收不到「story 停在门上」的信号)
- 关键跃迁 428 挂起(上线/结项 UI 门)→ 同样出 gate_waiting 行
- 意外异常 → 结构化 500(带 story_key/reasonCode),不是裸 traceback
- 预期失败(HTTPException 4xx)经包装层原样透传,不被吞成 500
"""

import json

import pytest
from fastapi.testclient import TestClient

from story_lifecycle.infra.db import models as db
from story_lifecycle.orchestrator.service.api import app
from story_lifecycle.orchestrator.service.routers import lifecycle as lc


@pytest.fixture
def client(isolated_story_home):
    return TestClient(app)


@pytest.fixture
def quiet_config(monkeypatch):
    """无 notification: 段的干净配置(路由走代码内默认,不受真实 config.yaml 影响)。"""
    monkeypatch.setattr("story_lifecycle.infra.config.get_config", lambda: {})


def _gate_waiting_rows(story_key: str) -> list[dict]:
    return [
        r
        for r in db.list_notifications()
        if r["event_type"] == "gate_waiting" and r["story_key"] == story_key
    ]


class TestGateWaitingOnAdvance:
    def test_gate_unsatisfied_409_and_outbox_row(
        self, client, isolated_story_home, quiet_config, monkeypatch
    ):
        """gate 未满足 → 409 且 outbox 出现 gate_waiting 行(payload 带缺失项)。"""
        db.upsert_story("GW-1", title="t", workspace="/tmp", profile="minimal")
        db.update_story("GW-1", intake_state="ready", lifecycle_state="开发")
        # 开发→测试 的 gate 是 code;直接判不满足,跳过昂贵的 git diff 回退。
        monkeypatch.setattr(
            "story_lifecycle.sourcing.deliverables.gate_satisfied",
            lambda sk, f, t: (False, ["代码变更"]),
        )

        r = client.post("/api/story/GW-1/lifecycle/advance")
        assert r.status_code == 409
        assert "代码变更" in r.json()["detail"]

        rows = _gate_waiting_rows("GW-1")
        assert len(rows) == 1
        row = rows[0]
        assert row["status"] == "pending"  # 只写 outbox 一行,投递归异步线程
        assert "代码变更" in row["message"]
        payload = json.loads(row["payload_json"])
        assert payload["from"] == "开发"
        assert payload["to"] == "测试"
        assert payload["missing"] == ["代码变更"]
        assert payload["reason"] == "gate_unsatisfied"

    def test_upgrade_pending_428_and_outbox_row(
        self, client, isolated_story_home, quiet_config, monkeypatch
    ):
        """关键跃迁 428 挂起(测试→上线)→ 同样出 gate_waiting 行。"""
        db.upsert_story("GW-2", title="t", workspace="/tmp", profile="minimal")
        db.update_story("GW-2", intake_state="ready", lifecycle_state="测试")
        monkeypatch.setattr(
            "story_lifecycle.sourcing.deliverables.gate_satisfied",
            lambda sk, f, t: (True, []),
        )

        r = client.post("/api/story/GW-2/lifecycle/advance")
        assert r.status_code == 428

        rows = _gate_waiting_rows("GW-2")
        assert len(rows) == 1
        payload = json.loads(rows[0]["payload_json"])
        assert payload["from"] == "测试"
        assert payload["to"] == "上线"
        assert payload["reason"] == "upgrade_pending"
        assert payload["origin"] == "advance"
        # 挂起态照常落地(emit 是纯观察,不碰状态)
        ctx = json.loads(db.get_story("GW-2")["context_json"])
        assert ctx["_upgrade_gate"]["target"] == "上线"
        assert db.get_story("GW-2")["lifecycle_state"] == "测试"


class TestAdvanceStructuredError:
    def test_unexpected_error_structured_500(
        self, client, isolated_story_home, monkeypatch
    ):
        """意外内部异常 → 结构化 500(带 story_key/reasonCode),非裸 traceback。"""

        def _boom(*args, **kwargs):
            raise RuntimeError("boom-inner")

        db.upsert_story("GW-3", title="t", workspace="/tmp", profile="minimal")
        db.update_story("GW-3", intake_state="ready", lifecycle_state="开发")
        monkeypatch.setattr(lc, "_run_advance_precheck", _boom)

        r = client.post("/api/story/GW-3/lifecycle/advance")
        assert r.status_code == 500
        body = r.json()
        assert body["ok"] is False
        assert body["reasonCode"] == "advance_internal_error"
        assert body["story_key"] == "GW-3"
        assert "boom-inner" in body["message"]

    def test_expected_4xx_passthrough_not_500(
        self, client, isolated_story_home
    ):
        """预期失败(HTTPException)经包装层原样透传——不被吞成 500。"""
        db.upsert_story("GW-4", title="t", workspace="/tmp", profile="minimal")

        r = client.post(
            "/api/story/GW-4/lifecycle/advance", json={"confirmed_via": "bogus"}
        )
        assert r.status_code == 400  # 不是 500

        r404 = client.post("/api/story/NONEXIST/lifecycle/advance")
        assert r404.status_code == 404  # 不是 500
