"""WP-C 回归 — TAPD 工单环:挂龄纯函数 + 同步升级事件(当日去重) + 日清 digest。

覆盖(DESIGN-v1-work-agent §5 验收线):
- aging 纯函数:created 5 天前 + in_progress → 5;reopened 取 modified;
  created 缺失 → None;story 逾期天数;阈值缺省 3 + config 覆盖
- fixture bug(created=now-5d)→ 同步后 outbox 恰好一行 bug_aging;
  同日再跑仍是一行(当日去重)
- POST /api/digest/daily → markdown 四节齐(含种子条目)+ outbox 有 daily_digest
- 路由表:daily_digest→digest、bug_aging/story_overdue→interrupt、
  digest 档通道含 wechat
"""

import json
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from story_lifecycle.infra.db import models as db
from story_lifecycle.infra.notification import router as notif_router
from story_lifecycle.orchestrator.service.api import app
from story_lifecycle.sourcing.aging import (
    DEFAULT_BUG_AGING_WARN_DAYS,
    bug_active_days,
    bug_aging_warn_days,
    bug_time_fields,
    story_overdue_days,
)
from story_lifecycle.sourcing.sources.base import SourceItem


@pytest.fixture
def client(isolated_story_home):
    return TestClient(app)


@pytest.fixture
def quiet_config(monkeypatch):
    """无 routes 覆盖 + 显式关闭免打扰的干净配置:interrupt 不被真实
    config.yaml 的免打扰时段降级,阈值不受真实 tapd 段污染。"""
    monkeypatch.setattr(
        "story_lifecycle.infra.config.get_config",
        lambda: {"notification": {"quiet_hours": None}},
    )


def _today():
    """与生产判定同轴的"今天"(UTC 日期,见 sourcing.aging._today)。"""
    return datetime.now(timezone.utc).date()


def _days_ago(n: int) -> str:
    return (_today() - timedelta(days=n)).isoformat()


def _outbox(event_type: str, story_key: str = "") -> list[dict]:
    rows = [
        r
        for r in db.list_notifications()
        if r["event_type"] == event_type
        and (not story_key or r["story_key"] == story_key)
    ]
    return rows


def _bug_item(item_id: str, *, created: str, status: str = "new", owner: str = "zhangsan") -> SourceItem:
    return SourceItem(
        id=item_id,
        source="tapd",
        item_type="bug",
        title=f"bug {item_id}",
        description="",
        owner=owner,
        status=status,
        extra={
            "severity": "严重",
            "url": f"https://www.tapd.cn/12345/bugtrace/bugs/view?bug_id={item_id}",
            "created": created,
            "modified": created,
        },
    )


class TestAgingPureFunctions:
    def test_bug_active_days_in_progress_uses_created(self):
        bug = {"created": _days_ago(5), "modified": _days_ago(1), "status": "in_progress"}
        assert bug_active_days(bug) == 5

    def test_bug_active_days_reopened_uses_modified(self):
        """reopened 的 bug 从最近一次激活(modified)算挂龄,不背历史包袱。"""
        bug = {"created": _days_ago(30), "modified": _days_ago(2), "status": "reopened"}
        assert bug_active_days(bug) == 2

    def test_bug_active_days_missing_created_returns_none(self):
        assert bug_active_days({"status": "new"}) is None
        assert bug_active_days({"created": "", "status": "new"}) is None
        assert bug_active_days({"created": "not-a-date", "status": "new"}) is None

    def test_bug_active_days_reopened_without_modified_falls_back_to_created(self):
        bug = {"created": _days_ago(7), "modified": "", "status": "reopened"}
        assert bug_active_days(bug) == 7

    def test_bug_active_days_future_created_clamped_to_zero(self):
        """未来 created(时钟偏差)不返回负数,钳 0。"""
        bug = {"created": _days_ago(-3), "status": "new"}
        assert bug_active_days(bug) == 0

    def test_story_overdue_days_math(self):
        assert story_overdue_days({"deadline": _days_ago(3)}) == 3
        # 到期当天不算逾期(未逾期 → None,不返回 0/负数)
        assert story_overdue_days({"deadline": _today().isoformat()}) is None
        assert story_overdue_days({"deadline": _days_ago(-5)}) is None
        assert story_overdue_days({}) is None
        assert story_overdue_days({"deadline": "garbage"}) is None

    def test_warn_days_default_and_config_override(self):
        assert bug_aging_warn_days({}) == DEFAULT_BUG_AGING_WARN_DAYS == 3
        assert bug_aging_warn_days({"tapd": {"bug_aging_warn_days": 7}}) == 7
        # 坏值/负值 → 缺省(坏配置不炸、不关提醒)
        assert bug_aging_warn_days({"tapd": {"bug_aging_warn_days": "abc"}}) == 3
        assert bug_aging_warn_days({"tapd": {"bug_aging_warn_days": -1}}) == 3
        assert bug_aging_warn_days({"tapd": {}}) == 3

    def test_warn_days_reads_config_yaml_when_no_explicit_config(self, monkeypatch):
        """config=None 走 infra.config.get_config(monkeypatch 可控)。"""
        monkeypatch.setattr(
            "story_lifecycle.infra.config.get_config",
            lambda: {"tapd": {"bug_aging_warn_days": 5}},
        )
        assert bug_aging_warn_days() == 5

    def test_bug_time_fields_reads_context_keys(self, isolated_story_home):
        """bug_time_fields 从 story 行的 context_json 还原入参(与落库同源)。"""
        story, _ = db.upsert_story_from_source(
            source_type="tapd",
            source_id="bug_9001",
            title="带时间字段",
            tapd_type="bug",
            tapd_status="reopened",
        )
        db.update_context(story["story_key"], "bug_created", _days_ago(30))
        db.update_context(story["story_key"], "bug_modified", _days_ago(4))
        fields = bug_time_fields(db.get_story(story["story_key"]))
        assert fields == {
            "created": _days_ago(30),
            "modified": _days_ago(4),
            "status": "reopened",
        }
        assert bug_active_days(fields) == 4


class TestNotificationRoutes:
    def test_default_routes_new_events(self):
        assert notif_router.DEFAULT_ROUTES["bug_aging"] == notif_router.TIER_INTERRUPT
        assert notif_router.DEFAULT_ROUTES["story_overdue"] == notif_router.TIER_INTERRUPT
        assert notif_router.DEFAULT_ROUTES["daily_digest"] == notif_router.TIER_DIGEST

    def test_digest_tier_channels_include_wechat(self):
        assert notif_router.TIER_CHANNELS[notif_router.TIER_DIGEST] == (
            "wechat",
            "desktop",
        )
        actions = notif_router.route(
            "daily_digest", config={"notification": {"quiet_hours": None}}
        )
        assert [a.channel for a in actions] == ["wechat", "desktop"]
        assert all(a.tier == notif_router.TIER_DIGEST for a in actions)

    def test_interrupt_events_route_wechat_desktop(self):
        for event in ("bug_aging", "story_overdue"):
            actions = notif_router.route(
                event, config={"notification": {"quiet_hours": None}}
            )
            assert [a.channel for a in actions] == ["wechat", "desktop"]
            assert all(a.tier == notif_router.TIER_INTERRUPT for a in actions)


class TestSyncServiceEscalation:
    """sync_service.sync_tapd 直调(CLI `story sync` 路径,不经 API):
    时间字段落 context_json + 超龄升级事件 emit 一次/天/key(当日去重)。

    WP-C 检查点返修:落库与升级收口进 sync_tapd —— CLI 主路径(story-loop
    step 1)与 API 路由同一实现,这里锁 CLI 直调路径的端到端行为。
    """

    def test_sync_tapd_persists_time_fields_and_emits_once_per_day(
        self, isolated_story_home, quiet_config
    ):
        """created=now-5d 的 bug → 直调 sync_tapd:context_json 落时间字段 +
        恰好一行 bug_aging;同日重跑(再调一次 sync_tapd)仍一行(去重)。"""
        from story_lifecycle.orchestrator.service.sync_service import sync_tapd

        item = _bug_item("bug_7001", created=_days_ago(5))
        sync_tapd([item], workspace="/tmp/ws")

        # 时间字段落 context_json(无 API 参与,纯 service 层)
        row = db.get_story("tapd-bug_7001")
        assert row is not None
        ctx = json.loads(row["context_json"])
        assert ctx["bug_created"] == _days_ago(5)
        assert ctx["bug_modified"] == _days_ago(5)

        rows = _outbox("bug_aging", "tapd-bug_7001")
        assert len(rows) == 1
        assert rows[0]["status"] == "pending"
        assert rows[0]["tier"] == "interrupt"
        payload = json.loads(rows[0]["payload_json"])
        assert payload["key"] == "tapd-bug_7001"
        assert payload["age_days"] == 5
        assert payload["url"].endswith("bug_id=bug_7001")
        assert payload["title"] == "bug bug_7001"

        # 同日重跑同步(真实 CLI 场景:每天跑一次 story sync)→ 幂等落库 + 去重
        sync_tapd([item], workspace="/tmp/ws")
        assert len(_outbox("bug_aging", "tapd-bug_7001")) == 1

    def test_young_bug_not_escalated(self, isolated_story_home, quiet_config):
        """挂龄 1 天 < 阈值 3 → 不发。"""
        from story_lifecycle.orchestrator.service.sync_service import sync_tapd

        item = _bug_item("bug_7002", created=_days_ago(1))
        sync_tapd([item], workspace="/tmp/ws")
        assert _outbox("bug_aging") == []

    def test_owner_mismatch_not_escalated(self, isolated_story_home, quiet_config):
        """配置了 owner 且 bug current_owner 不匹配 → 不发;匹配 → 发。

        tapd_config 显式传参(API 路由形态);不传时 sync_tapd 自取
        _load_tapd_config()(CLI 形态,isolated home 无配置 → {} 不过滤)。
        """
        from story_lifecycle.orchestrator.service.sync_service import sync_tapd

        item = _bug_item("bug_7003", created=_days_ago(9), owner="zhangsan")
        sync_tapd([item], workspace="/tmp/ws", tapd_config={"owner": "lisi;"})
        assert _outbox("bug_aging") == []
        sync_tapd([item], workspace="/tmp/ws", tapd_config={"owner": "zhangsan"})
        assert len(_outbox("bug_aging")) == 1

    def test_overdue_story_emits_story_overdue(self, isolated_story_home, quiet_config):
        from story_lifecycle.orchestrator.service.sync_service import sync_tapd

        item = SourceItem(
            id="7100",
            source="tapd",
            item_type="requirement",
            title="逾期需求",
            description="",
            deadline=_days_ago(4),
            extra={"url": "https://www.tapd.cn/12345/prong/stories/view/7100"},
        )
        sync_tapd([item], workspace="/tmp/ws")
        sync_tapd([item], workspace="/tmp/ws")  # 同日重跑 → 去重

        rows = _outbox("story_overdue", "tapd-7100")
        assert len(rows) == 1
        assert rows[0]["tier"] == "interrupt"
        payload = json.loads(rows[0]["payload_json"])
        assert payload["age_days"] == 4

        # 未逾期需求不发
        fresh = SourceItem(
            id="7101",
            source="tapd",
            item_type="requirement",
            title="未来需求",
            description="",
            deadline=_days_ago(-3),
            extra={},
        )
        sync_tapd([fresh], workspace="/tmp/ws")
        assert _outbox("story_overdue", "tapd-7101") == []

    def test_dry_run_does_not_persist_or_escalate(
        self, isolated_story_home, quiet_config
    ):
        """dry_run 只探路:不落时间字段、不发事件(「同步成功」前提)。"""
        from story_lifecycle.orchestrator.service.sync_service import sync_tapd

        item = _bug_item("bug_7004", created=_days_ago(6))
        result = sync_tapd([item], workspace="/tmp/ws", dry_run=True)
        assert result["would_create"] == 1
        assert db.get_story("tapd-bug_7004") is None
        assert _outbox("bug_aging") == []


class TestSyncApiWiring:
    def test_sync_api_persists_time_fields_and_escalates(
        self, client, isolated_story_home, quiet_config, monkeypatch, tmp_path
    ):
        """POST /api/sync/tapd(fake source)→ 落库/落时间字段/升级事件全部由
        sync_tapd 收口(router 只透传 tapd_config):bug 时间字段落 context_json +
        outbox 出 bug_aging/story_overdue 各一行;同日重 POST 仍各一行(去重)。
        """
        from story_lifecycle.orchestrator.service.routers import sync as sync_router
        from story_lifecycle.sourcing.sources import tapd_source as ts_mod

        items = [
            _bug_item("bug_8001", created=_days_ago(5)),
            SourceItem(
                id="8002",
                source="tapd",
                item_type="requirement",
                title="逾期需求8002",
                description="",
                deadline=_days_ago(2),
                extra={"url": "https://www.tapd.cn/12345/prong/stories/view/8002"},
            ),
        ]

        class _FakeApi:
            workspace_id = "12345"

            def get_related_bugs(self, story_id):
                return []

        class _FakeTapdSource:
            def __init__(self, config):
                self.owner = config.get("owner", "")
                self._api = _FakeApi()

            def fetch_pending(self, fetch_all=False, item_type=None):
                return list(items)

            def get_status_names(self):
                return {}

        monkeypatch.setattr(
            sync_router, "_load_tapd_config", lambda: {"workspace_id": "12345"}
        )
        monkeypatch.setattr(ts_mod, "TapdSource", _FakeTapdSource)

        body = {"workspace": str(tmp_path / "ws")}
        resp = client.post("/api/sync/tapd", json=body)
        assert resp.status_code == 200
        assert resp.json()["created"] == 2

        # 时间字段落 context_json(落点:update_context 约定,与 severity 不同 ——
        # severity 不落库、url 走 tapd_url 列)
        bug_row = db.get_story("tapd-bug_8001")
        assert bug_row is not None
        ctx = json.loads(bug_row["context_json"])
        assert ctx["bug_created"] == _days_ago(5)
        assert bug_row["tapd_url"].endswith("bug_id=bug_8001")

        # 升级事件各一行(interrupt 档)
        assert len(_outbox("bug_aging", "tapd-bug_8001")) == 1
        assert len(_outbox("story_overdue", "tapd-8002")) == 1

        # 同日重同步 → 时间字段幂等更新,告警当日去重仍各一行
        resp2 = client.post("/api/sync/tapd", json=body)
        assert resp2.status_code == 200
        assert len(_outbox("bug_aging", "tapd-bug_8001")) == 1
        assert len(_outbox("story_overdue", "tapd-8002")) == 1


class TestDigestDaily:
    def _seed(self):
        today = _today()
        # ① 明天到期的需求(candidate 池即可见)
        db.upsert_story_from_source(
            source_type="tapd",
            source_id="8101",
            title="明日到期需求",
            deadline=(today + timedelta(days=1)).isoformat(),
            tapd_url="https://www.tapd.cn/12345/prong/stories/view/8101",
        )
        # ② 超龄 bug:created = 10 天前(挂龄 10 ≥ 阈值 3)
        bug, _ = db.upsert_story_from_source(
            source_type="tapd",
            source_id="bug_8102",
            title="超龄白屏bug",
            tapd_type="bug",
            tapd_status="new",
            tapd_url="https://www.tapd.cn/12345/bugtrace/bugs/view?bug_id=8102",
        )
        db.update_context(bug["story_key"], "bug_created", _days_ago(10))
        # ③ active story(开发态 + ready)+ 最近 gate 记录
        act, _ = db.upsert_story_from_source(
            source_type="tapd", source_id="8103", title="进行中需求"
        )
        db.update_story(act["story_key"], intake_state="ready", lifecycle_state="开发")
        db.record_gate_result(act["story_key"], "design", "spec_quality", "PASS")
        return bug, act

    def _seed_patrol_fail_yesterday(self, story_key: str) -> int:
        yesterday = (_today() - timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S")
        run = db.create_patrol_run(
            story_key,
            run_scope="train:app-1.2.33",
            executor="ai:prod-patrol",
            summary="错误面突增",
            items=[
                {
                    "item_seq": 1,
                    "result": "FAIL",
                    "observed": "83条",
                    "evidence_ref": "patrol.md#r2",
                }
            ],
        )
        with db._db() as conn:
            conn.execute(
                "UPDATE patrol_run SET started_at = ? WHERE id = ?",
                (yesterday, run["id"]),
            )
        return run["id"]

    def test_digest_daily_sections_and_outbox(
        self, client, isolated_story_home, quiet_config
    ):
        bug, act = self._seed()
        run_id = self._seed_patrol_fail_yesterday(act["story_key"])

        resp = client.post("/api/digest/daily")
        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is True and body["emitted"] is True

        md = body["markdown"]
        # 四节齐
        assert "## ① 今日/近 3 日到期 story" in md
        assert "## ② 超龄 bug 排行" in md
        assert "## ③ 进行中 story" in md
        assert "## ④ 昨日巡检 FAIL" in md
        # 各节含种子条目
        assert "明日到期需求" in md
        assert "超龄白屏bug" in md and "10 天" in md
        assert act["story_key"] in md and "design/spec_quality:PASS" in md
        assert f"run #{run_id}" in md and "app-1.2.33" in md

        # outbox 有 daily_digest 行(digest 档,结构化 payload 带计数)
        rows = _outbox("daily_digest")
        assert len(rows) == 1
        assert rows[0]["tier"] == "digest"
        payload = json.loads(rows[0]["payload_json"])
        assert payload["counts"]["due_stories"] == 1
        assert payload["counts"]["aging_bugs"] == 1
        assert payload["sections"]["aging_bugs"][0]["story_key"] == bug["story_key"]
        assert payload["sections"]["patrol_fails"][0]["train"] == "app-1.2.33"

    def test_digest_builder_empty_db_no_crash(self, isolated_story_home):
        """空库不崩,四节都在(各 0 条)。"""
        from story_lifecycle.orchestrator.service.routers.digest import (
            build_daily_digest,
        )

        built = build_daily_digest()
        assert "## ① 今日/近 3 日到期 story — 0 条" in built["markdown"]
        assert "## ④ 昨日巡检 FAIL — 0 轮" in built["markdown"]
        assert built["sections"] == {
            "due_stories": [],
            "aging_bugs": [],
            "active_stories": [],
            "patrol_fails": [],
        }

    def test_digest_respects_threshold_override(self, isolated_story_home, monkeypatch):
        """阈值调大 → 低龄 bug 不进 ② 节(阈值消费 config 覆盖)。"""
        from story_lifecycle.orchestrator.service.routers.digest import (
            build_daily_digest,
        )

        bug, _ = db.upsert_story_from_source(
            source_type="tapd",
            source_id="bug_8201",
            title="低龄bug",
            tapd_type="bug",
            tapd_status="new",
        )
        db.update_context(bug["story_key"], "bug_created", _days_ago(4))
        monkeypatch.setattr(
            "story_lifecycle.infra.config.get_config",
            lambda: {"tapd": {"bug_aging_warn_days": 30}},
        )
        built = build_daily_digest()
        assert "低龄bug" not in built["markdown"]
        assert built["sections"]["aging_bugs"] == []


class TestDailyPushCli:
    def test_daily_push_reuses_shared_builder(self, isolated_story_home, quiet_config):
        """``story daily --push``:与 /api/digest/daily 共用 build_daily_digest,
        经 emit_event 落 outbox(digest 档),输出含四节 markdown。
        """
        from click.testing import CliRunner

        from story_lifecycle.entry.cli.daily_cmd import daily_cmd

        db.upsert_story_from_source(
            source_type="tapd",
            source_id="8301",
            title="简报里到期的需求",
            deadline=_days_ago(-1),
        )
        result = CliRunner().invoke(daily_cmd, ["--push"])
        assert result.exit_code == 0, result.output

        out = result.output
        assert "## ① 今日/近 3 日到期 story" in out
        assert "简报里到期的需求" in out
        assert "已推送 outbox #" in out

        rows = _outbox("daily_digest")
        assert len(rows) == 1
        assert rows[0]["tier"] == "digest"
        payload = json.loads(rows[0]["payload_json"])
        assert payload["counts"]["due_stories"] == 1

    def test_daily_default_mode_still_pure_read(self, isolated_story_home):
        """不带 --push 保持原行为:纯读,不写 outbox。"""
        from click.testing import CliRunner

        from story_lifecycle.entry.cli.daily_cmd import daily_cmd

        result = CliRunner().invoke(daily_cmd, ["--json"])
        assert result.exit_code == 0
        assert _outbox("daily_digest") == []
