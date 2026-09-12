"""WP-B 洞②回归 — POST /api/story/{key}/stages/{stage}/complete(设计 §4.2)。

动机(tapd-1069609 事故):外部 agent 没有任何 API 能写 context_json._completed_stages,
被逼直写 DB。deliverables 的 code gate 存在性只看这个键,deliverables/code/skip
的语义是「合法不需要」,不能顶替——必须给一个人工标记完成的正门。

决策表五行全覆盖 + append 后 GET /deliverables 的 code.exists 变 true。
"""

import json

import pytest
from fastapi.testclient import TestClient

from story_lifecycle.infra.db import models as db
from story_lifecycle.orchestrator.service.api import app


@pytest.fixture
def client(isolated_story_home):
    return TestClient(app)


def _mk_story(key: str, **kw) -> str:
    """建 story。注意 upsert_story 的 INSERT 路径会丢额外 kwargs,
    所以 lifecycle_state 等字段走 update_story 补写。"""
    db.upsert_story(
        key,
        title=key,
        workspace="/tmp",
        profile="minimal",
        status=kw.pop("status", "active"),
        current_stage=kw.pop("current_stage", "design"),
    )
    db.update_story(key, intake_state="ready", **kw)
    return key


def _manual_events(key: str) -> list[dict]:
    return [
        e for e in db.get_story_events(key)
        if e["event_type"] == "stage_completed_manual"
    ]


class TestStageCompleteDecisionTable:
    """设计 §4.2 决策表:404 / 409 终态 / 400 非法 stage / 200 幂等 / 200 append。"""

    def test_404_nonexistent_story(self, client):
        r = client.post("/api/story/NOPE/stages/build/complete")
        assert r.status_code == 404

    def test_409_terminal_lifecycle_state(self, client, isolated_story_home):
        """终态(lifecycle 已到结项,无下一态)→ 409。"""
        key = _mk_story("SC-TERM", lifecycle_state="结项")
        r = client.post(f"/api/story/{key}/stages/build/complete")
        assert r.status_code == 409
        assert "终态" in r.json()["detail"]
        # 未写入
        ctx = json.loads(db.get_story(key)["context_json"] or "{}")
        assert "_completed_stages" not in ctx

    def test_409_terminal_engine_status(self, client, isolated_story_home):
        """终态(引擎已 completed)→ 409。"""
        key = _mk_story("SC-DONE", status="completed")
        r = client.post(f"/api/story/{key}/stages/build/complete")
        assert r.status_code == 409

    def test_400_unknown_stage_lists_legal_stages(self, client, isolated_story_home):
        """stage ∉ profile stages → 400,响应列出合法 stages。"""
        key = _mk_story("SC-BAD")
        r = client.post(f"/api/story/{key}/stages/bogus/complete")
        assert r.status_code == 400
        detail = r.json()["detail"]
        for legal in ("design", "build", "verify"):  # minimal profile 的三阶段
            assert legal in detail
        assert "bogus" in detail

    def test_200_idempotent_no_duplicate_append_or_event(
        self, client, isolated_story_home
    ):
        """已在 _completed_stages → 200 幂等:不重复 append、不重复事件,回 gate。"""
        key = _mk_story("SC-IDEM")
        db.update_story(
            key,
            context_json=json.dumps({"_completed_stages": ["build"]},
                                    ensure_ascii=False),
        )

        r = client.post(
            f"/api/story/{key}/stages/build/complete",
            json={"evidence": {"note": "重复标记"}},
        )
        assert r.status_code == 200
        body = r.json()
        assert body["already_completed"] is True
        assert body["completed_stages"] == ["build"]
        assert "gate" in body  # 幂等行也回 gate 状态

        ctx = json.loads(db.get_story(key)["context_json"])
        assert ctx["_completed_stages"] == ["build"]  # 不重复 append
        assert _manual_events(key) == []  # 不重复事件

    def test_200_append_event_and_gate_summary(self, client, isolated_story_home):
        """合法未完成 → append + stage_completed_manual 事件 + 200 gate 摘要。"""
        key = _mk_story("SC-NEW")
        r = client.post(
            f"/api/story/{key}/stages/build/complete",
            json={"evidence": {"note": "外部 agent 完成", "commits": ["abc123"]}},
        )
        assert r.status_code == 200
        body = r.json()
        assert body["ok"] is True
        assert body["already_completed"] is False
        assert body["completed_stages"] == ["build"]
        # gate 摘要反映当前状态(待启动 → 开发,要求 prd+spec)
        assert body["gate"]["from"] == "待启动"
        assert body["gate"]["to"] == "开发"

        # 事件落地,payload 带 evidence
        events = _manual_events(key)
        assert len(events) == 1
        assert events[0]["stage"] == "build"
        payload = db.parse_event_payload(events[0])
        assert payload["stage"] == "build"
        assert payload["evidence"]["commits"] == ["abc123"]
        assert payload["evidence"]["note"] == "外部 agent 完成"

        # append 真的持久化
        ctx = json.loads(db.get_story(key)["context_json"])
        assert ctx["_completed_stages"] == ["build"]

    def test_code_gate_exists_true_after_append(self, client, isolated_story_home):
        """1069609 事故本体:append build 后 GET /deliverables 的 code.exists=true。"""
        key = _mk_story("SC-CODE")
        before = client.get(f"/api/story/{key}/deliverables").json()["deliverables"]
        code_before = [d for d in before if d["key"] == "code"][0]
        assert code_before["exists"] is False

        r = client.post(f"/api/story/{key}/stages/build/complete")
        assert r.status_code == 200

        after = client.get(f"/api/story/{key}/deliverables").json()["deliverables"]
        code_after = [d for d in after if d["key"] == "code"][0]
        assert code_after["exists"] is True
