"""Test intake_state boundary — candidate/ready guards."""

from story_lifecycle.infra.db import models as db
from story_lifecycle.orchestrator.engine.graph import start_story_async, recover_orphan_stories


class TestCandidateRejection:
    def test_sync_creates_candidate_idle(self, isolated_story_home):
        story, created = db.upsert_story_from_source(
            "tapd",
            "99999",
            title="Test Candidate",
            workspace=str(isolated_story_home),
        )
        assert created
        assert story["intake_state"] == "candidate"
        # idle 移出 status:candidate 的"未启动"由 intake_state=candidate 表达,
        # status 用 active(candidate 被 intake_state 过滤挡在四 tab 外)。
        assert story["status"] == "active"

    def test_start_story_async_rejects_candidate(
        self, isolated_story_home, monkeypatch
    ):
        key = "tapd-99998"
        db.create_story(key, "Test Reject", str(isolated_story_home))
        db.update_story(
            key, intake_state="candidate", source_type="tapd", source_id="99998"
        )
        monkeypatch.setattr("story_lifecycle.orchestrator.engine.graph._running_stories", {})
        start_story_async(key)
        from story_lifecycle.orchestrator.engine.graph import is_story_running

        assert not is_story_running(key)

    def test_list_active_stories_excludes_candidates(self, isolated_story_home):
        db.create_story("ready-1", "Ready Story", str(isolated_story_home))
        db.update_story("ready-1", intake_state="ready")
        db.create_story("cand-1", "Candidate Story", str(isolated_story_home))
        db.update_story("cand-1", intake_state="candidate")
        active = db.list_active_stories()
        keys = [s["story_key"] for s in active]
        assert "ready-1" in keys
        assert "cand-1" not in keys

    def test_recover_orphan_marks_paused_not_resumed(
        self, isolated_story_home, monkeypatch
    ):
        """Restart recovery must NOT auto-resume (re-launch the AI CLI). It marks
        ready orphans 'paused' for manual resume; candidates are untouched."""
        db.create_story("ready-orphan", "Ready Orphan", str(isolated_story_home))
        db.update_story("ready-orphan", intake_state="ready", status="active")
        db.create_story("cand-orphan", "Candidate Orphan", str(isolated_story_home))
        db.update_story("cand-orphan", intake_state="candidate", status="active")
        resumed = []
        monkeypatch.setattr(
            "story_lifecycle.orchestrator.engine.graph.resume_story_async",
            lambda k: resumed.append(k),
        )
        recover_orphan_stories()
        # Nothing is auto-resumed (no CLI relaunch on restart).
        assert resumed == []
        # The ready orphan is surfaced as paused for manual '继续执行'.
        assert db.get_story("ready-orphan")["status"] == "paused"


class TestListCompletedLimit:
    """list_completed_stories: limit 默认 None=全量(前端 DonePage 分页,后端不截断)。"""

    def _seed_done(self, n: int) -> None:
        for i in range(n):
            db.create_story(f"done-{i}", f"Done {i}", "/tmp")
            db.update_story(f"done-{i}", lifecycle_state="结项", status="completed")

    def test_default_returns_all(self, isolated_story_home):
        self._seed_done(25)
        rows = db.list_completed_stories()
        assert len(rows) == 25  # 默认不截断(原 limit=20 会只回 20 条)

    def test_explicit_limit_truncates(self, isolated_story_home):
        self._seed_done(25)
        rows = db.list_completed_stories(limit=10)
        assert len(rows) == 10


class TestCompletedFilterExemption:
    """COMPLETED_STATES(tapd closed)过滤只作用于非结项池 — 结项 story 豁免。"""

    def test_done_with_tapd_closed_visible_by_default(self, isolated_story_home):
        """结项 + tapd closed:默认列表(show_completed=False)仍可见。"""
        db.create_story("done-closed", "Done Closed", "/tmp")
        db.update_story(
            "done-closed",
            lifecycle_state="结项",
            status="completed",
            tapd_status="closed",
        )
        keys = [s["story_key"] for s in db.list_visible_stories()]
        assert "done-closed" in keys

    def test_active_with_tapd_closed_still_hidden(self, isolated_story_home):
        """开发态 + tapd closed:仍被隐藏(过滤收窄到非结项,没整体删除)。"""
        db.create_story("active-closed", "Active Closed", "/tmp")
        db.update_story(
            "active-closed",
            lifecycle_state="开发",
            status="active",
            intake_state="ready",
            tapd_status="closed",
        )
        keys = [s["story_key"] for s in db.list_visible_stories()]
        assert "active-closed" not in keys


class TestListVisibleDedup:
    """list_visible_stories 三档拼接去重 — candidate + completed 重叠时不重复。"""

    def test_candidate_and_closed_not_duplicated(self, isolated_story_home):
        """一条 intake=candidate 且 lifecycle=结项 的 story:同时命中 candidate 档和
        completed 档,只应出现一次(否则已结项 tab 里会有重复卡片)。"""
        db.create_story("dup-1", "Dup Candidate Done", "/tmp")
        db.update_story(
            "dup-1",
            intake_state="candidate",
            lifecycle_state="结项",
            status="completed",
        )
        keys = [s["story_key"] for s in db.list_visible_stories()]
        assert keys.count("dup-1") == 1
