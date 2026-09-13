"""WP-B 洞③回归 — /lifecycle/advance 停门管家事件 + 结构化 500。

- gate 未满足 → 409 且 notification_outbox 落 gate_waiting 行(此前静默 409,
  管家/微信侧收不到「story 停在门上」的信号)
- 409 响应体自描述(DESIGN-v1-work-agent 去重路径 v1.1):message + missing
  + remediation(补救端点序列),skill 不再手抄 gate 契约
- 关键跃迁 428 挂起(上线/结项 UI 门)→ 同样出 gate_waiting 行,响应体带
  ui-upgrade confirm 的 remediation
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
            "story_lifecycle.sourcing.deliverables.gate_missing",
            lambda sk, f, t: (False, ["code"], ["代码变更"]),
        )

        r = client.post("/api/story/GW-1/lifecycle/advance")
        assert r.status_code == 409
        # 自描述 409:detail 是 dict,message 保住人读主信息
        detail = r.json()["detail"]
        assert isinstance(detail, dict)
        assert "代码变更" in detail["message"]
        assert "成果物 gate 未满足" in detail["message"]
        assert detail["missing"] == ["code"]
        assert detail["remediation"][0]["gap"] == "code"

        rows = _gate_waiting_rows("GW-1")
        assert len(rows) == 1
        row = rows[0]
        assert row["status"] == "pending"  # 只写 outbox 一行,投递归异步线程
        assert "代码变更" in row["message"]
        payload = json.loads(row["payload_json"])
        assert payload["from"] == "开发"
        assert payload["to"] == "测试"
        assert payload["missing"] == ["code"]
        assert payload["missing_labels"] == ["代码变更"]
        assert payload["remediation"] == detail["remediation"]
        assert payload["reason"] == "gate_unsatisfied"

    def test_upgrade_pending_428_and_outbox_row(
        self, client, isolated_story_home, quiet_config, monkeypatch
    ):
        """关键跃迁 428 挂起(测试→上线)→ 同样出 gate_waiting 行。"""
        db.upsert_story("GW-2", title="t", workspace="/tmp", profile="minimal")
        db.update_story("GW-2", intake_state="ready", lifecycle_state="测试")
        monkeypatch.setattr(
            "story_lifecycle.sourcing.deliverables.gate_missing",
            lambda sk, f, t: (True, [], []),
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


class TestSelfDescribingGateResponses:
    """v1.1 去重路径:409/428 响应自描述,skill 循环「补缺口 → advance」。

    remediation map 的语义对齐(端点 per gap)在 sourcing/deliverables.py
    GAP_REMEDIATION;这里端到端验证 advance 真实 gate 路径吐出的形状。
    """

    def test_409_code_gap_carries_remediation(
        self, client, isolated_story_home, quiet_config
    ):
        """开发→测试 gate(code 缺)→ 409 missing=["code"] + 直报/确认补救端点。"""
        db.upsert_story("GW-10", title="t", workspace="/tmp", profile="minimal")
        db.update_story("GW-10", intake_state="ready", lifecycle_state="开发")

        r = client.post("/api/story/GW-10/lifecycle/advance")
        assert r.status_code == 409
        detail = r.json()["detail"]
        assert detail["missing"] == ["code"]
        entry = detail["remediation"][0]
        assert entry["gap"] == "code"
        endpoints = [(s["method"], s["endpoint"]) for s in entry["steps"]]
        assert (
            "POST",
            "/api/story/{key}/stages/{stage}/complete",
        ) in endpoints
        assert (
            "POST",
            "/api/story/{key}/deliverables/code/confirm",
        ) in endpoints
        # 合法不需要时的跳过出口
        alt = entry["alternative"]
        assert alt["endpoint"] == "/api/story/{key}/deliverables/code/skip"
        assert "合法不需要时" in alt["hint"]

    def test_409_test_report_gap_carries_remediation(
        self, client, isolated_story_home, quiet_config
    ):
        """测试→上线 gate(test_report 缺)→ 409 + PUT docs / confirm 补救端点。"""
        db.upsert_story("GW-11", title="t", workspace="/tmp", profile="minimal")
        db.update_story("GW-11", intake_state="ready", lifecycle_state="测试")

        r = client.post("/api/story/GW-11/lifecycle/advance")
        assert r.status_code == 409
        detail = r.json()["detail"]
        assert detail["missing"] == ["test_report"]
        entry = detail["remediation"][0]
        assert entry["gap"] == "test_report"
        endpoints = [(s["method"], s["endpoint"]) for s in entry["steps"]]
        assert ("PUT", "/api/story/{key}/docs/test_report") in endpoints
        assert ("PUT", "/api/story/{key}/docs/test_report/confirm") in endpoints
        assert entry["alternative"]["method"] == "POST"
        assert "skip" in entry["alternative"]["endpoint"]

    def test_409_pending_gate_to_dev_covers_prd_and_spec(
        self, client, isolated_story_home, quiet_config
    ):
        """待启动→开发 gate(prd+spec 都缺)→ missing 两项,remediation 逐 gap 对齐。"""
        db.upsert_story("GW-12", title="t", workspace="/tmp", profile="minimal")
        db.update_story("GW-12", intake_state="ready")  # lifecycle_state=待启动

        r = client.post("/api/story/GW-12/lifecycle/advance")
        assert r.status_code == 409
        detail = r.json()["detail"]
        assert detail["missing"] == ["prd", "spec"]
        by_gap = {e["gap"]: e for e in detail["remediation"]}
        assert set(by_gap) == {"prd", "spec"}
        assert by_gap["prd"]["steps"][0]["endpoint"] == "/api/story/{key}/docs/prd"
        assert (
            by_gap["spec"]["steps"][1]["endpoint"]
            == "/api/story/{key}/docs/spec/confirm"
        )

    def test_428_upgrade_pending_carries_ui_upgrade_remediation(
        self, client, isolated_story_home, quiet_config, monkeypatch
    ):
        """428 响应体带 ui-upgrade confirm 的 remediation(CLI 断路口自描述)。"""
        db.upsert_story("GW-13", title="t", workspace="/tmp", profile="minimal")
        db.update_story("GW-13", intake_state="ready", lifecycle_state="测试")
        monkeypatch.setattr(
            "story_lifecycle.sourcing.deliverables.gate_missing",
            lambda sk, f, t: (True, [], []),
        )

        r = client.post("/api/story/GW-13/lifecycle/advance")
        assert r.status_code == 428
        detail = r.json()["detail"]
        assert detail["action"] == "ui_confirm"  # 旧字段不破坏
        (step,) = detail["remediation"]
        assert step["method"] == "POST"
        assert step["endpoint"] == "/api/story/{key}/lifecycle/ui-upgrade"
        assert step["body"] == {"action": "confirm"}

    def test_remediation_map_covers_all_gate_keys(self):
        """GAP_REMEDIATION 覆盖 LIFECYCLE_GATES 的全部 gate key(防新 gate 漏配)。"""
        from story_lifecycle.sourcing.deliverables import (
            GAP_REMEDIATION,
            LIFECYCLE_GATES,
            remediation_for_gaps,
        )

        gate_keys = {k for keys in LIFECYCLE_GATES.values() for k in keys}
        assert gate_keys <= set(GAP_REMEDIATION)
        steps = remediation_for_gaps(sorted(gate_keys))
        assert [s["gap"] for s in steps] == sorted(gate_keys)
        for entry in steps:
            assert entry["steps"]
            assert entry["alternative"]["endpoint"].endswith("/skip")


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
