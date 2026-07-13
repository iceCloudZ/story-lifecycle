"""API integration tests — test FastAPI endpoints added in Phase 3.

Covers: Timeline, Gate History, Loop Trace, Findings, Dependency Graph,
Patterns, and boundary conditions.
"""

import pytest

from story_lifecycle.infra.db import models as db


@pytest.fixture
def api_client(isolated_story_home):
    """Create a FastAPI TestClient with isolated DB."""
    from story_lifecycle.orchestrator.service.api import app
    from fastapi.testclient import TestClient

    return TestClient(app)


@pytest.fixture
def seeded_story(isolated_story_home):
    """Create a story with events for API testing."""
    db.upsert_story(
        "API-TEST-001",
        title="API Test Story",
        workspace="/tmp/test-ws",
        profile="minimal",
        current_stage="implement",
        status="active",
    )
    # Add some events
    db.log_event("API-TEST-001", "design", "execute", {"adapter": "claude"})
    db.log_event("API-TEST-001", "design", "complete", {"summary": "Design done"})
    db.log_event("API-TEST-001", "implement", "execute", {"adapter": "claude"})
    return "API-TEST-001"


class TestTimelineAPI:
    def test_timeline_returns_stages(self, api_client, seeded_story):
        resp = api_client.get(f"/api/story/{seeded_story}/timeline")
        assert resp.status_code == 200
        data = resp.json()
        assert data["story_key"] == seeded_story
        assert isinstance(data["stages"], list)

    def test_timeline_nonexistent_story(self, api_client, isolated_story_home):
        resp = api_client.get("/api/story/NONEXIST/timeline")
        assert resp.status_code == 404


class TestGateHistoryAPI:
    def test_gate_history_returns_decisions(self, api_client, seeded_story):
        resp = api_client.get(f"/api/story/{seeded_story}/gate-history")
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data.get("decisions", []), list)

    def test_gate_history_empty(self, api_client, isolated_story_home):
        db.upsert_story("EMPTY-001", title="Empty", workspace="/tmp", profile="minimal")
        resp = api_client.get("/api/story/EMPTY-001/gate-history")
        assert resp.status_code == 200
        data = resp.json()
        assert data.get("decisions", []) == []

    def test_gate_history_nonexistent(self, api_client, isolated_story_home):
        resp = api_client.get("/api/story/NONEXIST/gate-history")
        assert resp.status_code == 404


class TestLoopTraceAPI:
    def test_loop_trace_returns_rounds(self, api_client, seeded_story):
        resp = api_client.get(f"/api/story/{seeded_story}/loop-trace")
        assert resp.status_code == 200
        data = resp.json()
        assert "plan_loop" in data or "code_loop" in data

    def test_loop_trace_nonexistent(self, api_client, isolated_story_home):
        resp = api_client.get("/api/story/NONEXIST/loop-trace")
        assert resp.status_code == 404


class TestStoryStatsAPI:
    def test_stats_aggregates_counts(
        self, api_client, seeded_story, isolated_story_home
    ):
        # two adversarial loop rounds (plan + review); a plain execute event must not count
        db.log_event(
            seeded_story, "design", "plan", {"adversarial_loop": True, "loop_rounds": 1}
        )
        db.log_event(
            seeded_story,
            "implement",
            "review",
            {"adversarial_loop": True, "loop_rounds": 1},
        )
        # two findings, one resolved → only the open one counts
        open_fid = db.create_finding(
            story_key=seeded_story,
            stage="implement",
            source="code_review",
            severity="high",
            category="security",
            description="SQL injection",
        )
        resolved_fid = db.create_finding(
            story_key=seeded_story,
            stage="implement",
            source="code_review",
            severity="low",
            category="style",
            description="nit",
        )
        db.update_finding(resolved_fid, status="resolved")
        # one delivery artifact (code change)
        db.create_delivery_artifact(
            story_key=seeded_story,
            kind="pr",
            provider="github",
            external_id="42",
            url="https://example.com/pr/42",
        )

        resp = api_client.get(f"/api/story/{seeded_story}/stats")
        assert resp.status_code == 200
        data = resp.json()
        assert data["code_changes"] == 1
        assert data["loop_rounds"] == 2
        assert data["findings_open"] == 1
        assert "tokens" in data
        # sanity: the open finding is the one we kept open
        assert open_fid != resolved_fid

    def test_stats_empty_story(self, api_client, isolated_story_home):
        db.upsert_story(
            "STATS-EMPTY", title="Empty", workspace="/tmp", profile="minimal"
        )
        resp = api_client.get("/api/story/STATS-EMPTY/stats")
        assert resp.status_code == 200
        data = resp.json()
        assert data["code_changes"] == 0
        assert data["loop_rounds"] == 0
        assert data["findings_open"] == 0
        assert "tokens" in data

    def test_stats_nonexistent(self, api_client, isolated_story_home):
        resp = api_client.get("/api/story/NONEXIST/stats")
        assert resp.status_code == 404


class TestWSStoryListJSON:
    def test_ws_push_includes_filter_fields(self, isolated_story_home):
        """Regression for the dashboard-zero-stories bug: the WS-pushed story
        list (which seeds the Dashboard's store/initialData) must include
        tapdType/intakeState — the Dashboard filters on them. _story_list_json
        previously returned only 7 fields, so the filters matched nothing."""
        from story_lifecycle.orchestrator.service.api import _story_list_json

        db.upsert_story(
            "WS-TEST-001",
            title="WS Test",
            workspace="/tmp",
            profile="minimal",
            status="active",
            tapd_type="story",
            intake_state="ready",
        )
        items = _story_list_json()
        ours = [s for s in items if s["storyKey"] == "WS-TEST-001"]
        assert ours, "story should appear in the WS-pushed list"
        assert ours[0]["tapdType"] == "story"
        assert ours[0]["intakeState"] == "ready"

    def test_ws_push_matches_rest_shape(self, api_client, isolated_story_home):
        """The WS list and the REST /api/story list must serialize identically."""
        from story_lifecycle.orchestrator.service.api import _story_list_json

        db.upsert_story(
            "WS-TEST-002",
            title="Shape Parity",
            workspace="/tmp",
            profile="minimal",
            status="active",
            tapd_type="bug",
            intake_state="ready",
        )
        ws_keys = {k for s in _story_list_json() for k in s}
        rest_keys = {k for s in api_client.get("/api/story").json() for k in s}
        assert ws_keys == rest_keys


class TestStartStoryWorkspace:
    def test_start_sets_workspace_to_project_repo(
        self, api_client, isolated_story_home, tmp_path
    ):
        """Regression: /start must point the story's workspace at the bound project's
        repo_path, so the AI CLI runs there. Previously workspace stayed the sync-time
        default (e.g. the orchestrator's own repo)."""
        repo = tmp_path / "myproj-repo"
        repo.mkdir()
        proj = db.create_project("myproj", str(repo), default_branch="main")
        db.upsert_story(
            "CAND-001",
            title="Cand",
            workspace="/old/default/ws",
            profile="minimal",
            status="idle",
            intake_state="candidate",
        )
        # upsert_story's INSERT branch hardcodes intake_state='ready', so set
        # candidate explicitly (mirrors how TAPD sync creates candidate stories).
        db.update_story("CAND-001", intake_state="candidate")
        resp = api_client.post(
            "/api/story/CAND-001/start",
            json={"project_ids": [proj["id"]], "content": "# PRD\n做登录记录查询"},
        )
        assert resp.status_code == 200
        s = db.get_story("CAND-001")
        assert s["workspace"] == str(repo.resolve())
        assert s["intake_state"] == "ready"
        assert s["status"] == "planning"

    def test_start_requires_content(self, api_client, isolated_story_home, tmp_path):
        """开始开发必须填写 story 内容/PRD。"""
        repo = tmp_path / "p2"
        repo.mkdir()
        proj = db.create_project("p2", str(repo), default_branch="main")
        db.upsert_story(
            "CAND-002",
            title="x",
            workspace=str(tmp_path),
            profile="minimal",
            status="idle",
            intake_state="candidate",
        )
        db.update_story("CAND-002", intake_state="candidate")
        resp = api_client.post(
            "/api/story/CAND-002/start", json={"project_ids": [proj["id"]]}
        )
        assert resp.status_code == 409
        assert resp.json()["reasonCode"] == "content_required"

    def test_start_saves_prd_and_path(self, api_client, isolated_story_home, tmp_path):
        """填了 content 后,/start 把 PRD 写到 story 证据目录并存 prd_path。"""
        proj = db.create_project("p3", str(tmp_path / "repo"), default_branch="main")
        (tmp_path / "repo").mkdir()
        db.upsert_story(
            "CAND-003",
            title="y",
            workspace=str(tmp_path / "repo"),
            profile="minimal",
            status="idle",
            intake_state="candidate",
        )
        db.update_story("CAND-003", intake_state="candidate")
        resp = api_client.post(
            "/api/story/CAND-003/start",
            json={"project_ids": [proj["id"]], "content": "# 需求\n登录记录查询"},
        )
        assert resp.status_code == 200
        import json as _json

        ctx = _json.loads(db.get_story("CAND-003")["context_json"] or "{}")
        prd_path = ctx.get("prd_path", "")
        from pathlib import Path

        pp = Path(prd_path)
        assert pp.name == "PRD.md"
        assert pp.parent.name == "003-y"
        assert pp.parent.parent.name == "story"
        assert "登录记录查询" in pp.read_text(encoding="utf-8")

    def test_start_ready_story_binds_project_and_enters_planning(
        self, api_client, isolated_story_home, tmp_path
    ):
        """Manual stories use the same one-step intake/start flow as TAPD stories."""
        repo = tmp_path / "manual-repo"
        repo.mkdir()
        proj = db.create_project("manual-proj", str(repo), default_branch="main")
        db.upsert_story(
            "MANUAL-001",
            title="手工需求",
            workspace=str(isolated_story_home),
            profile="minimal",
            status="idle",
            intake_state="ready",
        )

        resp = api_client.post(
            "/api/story/MANUAL-001/start",
            json={"project_ids": [proj["id"]], "content": "# PRD\n手工需求内容"},
        )

        assert resp.status_code == 200
        story = db.get_story("MANUAL-001")
        assert story["workspace"] == str(repo)
        assert story["status"] == "planning"
        assert story["intake_state"] == "ready"


class TestBuildCliPromptPrd:
    def test_prd_path_injected_not_content(self, tmp_path):
        """Regression: _build_cli_prompt must inject the PRD file PATH (not inline
        the content — that would blow up the CLI's context). LangGraph→Agent FC
        migration dropped PRD injection entirely."""
        from story_lifecycle.orchestrator.engine.planner import _build_cli_prompt

        prd = tmp_path / "prd" / "X.md"
        prd.parent.mkdir()
        prd.write_text("# 需求\n登录记录查询 字段：用户/IP/时间", encoding="utf-8")
        prompt = _build_cli_prompt(
            story_key="X",
            title="T",
            stage="design",
            focus="要点",
            done_file=".story-done/X-design.json",
            profile_stages={},
            prd_path=str(prd),
        )
        assert "PRD / 需求详情" in prompt
        assert str(prd) in prompt  # path injected
        assert "登录记录查询" not in prompt  # content NOT inlined (no context bloat)

    def test_no_prd_section_when_path_empty(self):
        from story_lifecycle.orchestrator.engine.planner import _build_cli_prompt

        prompt = _build_cli_prompt(
            story_key="X",
            title="T",
            stage="design",
            focus="要点",
            done_file="d",
            profile_stages={},
            prd_path="",
        )
        assert "PRD / 需求详情" not in prompt


class TestBuildCliPromptTranscript:
    def test_transcript_section_injected(self):
        from story_lifecycle.orchestrator.engine.planner import _build_cli_prompt

        prompt = _build_cli_prompt(
            story_key="X",
            title="T",
            stage="design",
            focus="要点",
            done_file="d",
            profile_stages={},
            transcript_section=(
                "### 历史上下文（来自既往 transcript）\n- 曾调研 hc-user 模块"
            ),
        )
        assert "历史上下文" in prompt
        assert "曾调研 hc-user" in prompt

    def test_no_transcript_section_when_empty(self):
        from story_lifecycle.orchestrator.engine.planner import _build_cli_prompt

        prompt = _build_cli_prompt(
            story_key="X",
            title="T",
            stage="design",
            focus="要点",
            done_file="d",
            profile_stages={},
            transcript_section="",
        )
        assert "历史上下文" not in prompt


class TestFindingsAPI:
    def test_findings_returns_dict(self, api_client, seeded_story, isolated_story_home):
        db.create_finding(
            story_key=seeded_story,
            stage="implement",
            source="code_review",
            severity="high",
            category="security",
            description="SQL injection risk",
        )
        resp = api_client.get(f"/api/story/{seeded_story}/findings")
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data, dict)
        assert "findings" in data

    def test_findings_with_filter(self, api_client, seeded_story, isolated_story_home):
        db.create_finding(
            story_key=seeded_story,
            stage="implement",
            source="code_review",
            severity="low",
            category="style",
            description="Code style issue",
        )
        resp = api_client.get(f"/api/story/{seeded_story}/findings?min_severity=high")
        assert resp.status_code == 200

    def test_findings_low_severity_returnable(
        self, api_client, seeded_story, isolated_story_home
    ):
        """Regression: low-severity open findings must be obtainable. Previously
        get_open_findings' default min_severity='medium' silently dropped them, so
        both the default list and ?min_severity=low returned no low findings."""
        db.create_finding(
            story_key=seeded_story,
            stage="implement",
            source="code_review",
            severity="low",
            category="style",
            description="minor nit",
        )
        default_resp = api_client.get(f"/api/story/{seeded_story}/findings")
        assert default_resp.status_code == 200
        assert "low" in [f["severity"] for f in default_resp.json()["findings"]]

        low_resp = api_client.get(
            f"/api/story/{seeded_story}/findings?min_severity=low"
        )
        assert low_resp.status_code == 200
        assert "low" in [f["severity"] for f in low_resp.json()["findings"]]

    def test_findings_empty(self, api_client, isolated_story_home):
        db.upsert_story(
            "NO-FIND-001", title="No Findings", workspace="/tmp", profile="minimal"
        )
        resp = api_client.get("/api/story/NO-FIND-001/findings")
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data, dict)
        assert data.get("findings", []) == []


class TestDependencyGraphAPI:
    def test_dependency_graph_returns_structure(self, api_client, seeded_story):
        resp = api_client.get(f"/api/story/{seeded_story}/dependency-graph")
        assert resp.status_code == 200
        data = resp.json()
        assert "nodes" in data
        assert "edges" in data

    def test_dependency_graph_nonexistent(self, api_client, isolated_story_home):
        resp = api_client.get("/api/story/NONEXIST/dependency-graph")
        assert resp.status_code == 404


class TestPatternsAPI:
    def test_patterns_list(self, api_client, isolated_story_home):
        resp = api_client.get("/api/patterns")
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data, dict)
        assert "patterns" in data

    def test_patterns_approve(self, api_client, isolated_story_home):
        pid = db.create_learned_pattern(
            pattern="Always validate inputs",
            applies_to=["api"],
            rule="Add input validation to all API endpoints",
            confidence="medium",
        )
        resp = api_client.post(f"/api/patterns/{pid}/approve")
        assert resp.status_code == 200

    def test_patterns_reject(self, api_client, isolated_story_home):
        pid = db.create_learned_pattern(
            pattern="Bad pattern",
            applies_to=["none"],
            rule="Do nothing",
            confidence="low",
        )
        resp = api_client.post(f"/api/patterns/{pid}/reject")
        assert resp.status_code == 200


class TestStoriesAPI:
    def test_list_stories(self, api_client, seeded_story):
        resp = api_client.get("/api/story")
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data, list)
        assert any(s.get("storyKey") == seeded_story for s in data)

    def test_get_single_story(self, api_client, seeded_story):
        resp = api_client.get(f"/api/story/{seeded_story}")
        assert resp.status_code == 200
        data = resp.json()
        # API returns snake_case or camelCase depending on serialization
        key = data.get("story_key") or data.get("storyKey")
        assert key == seeded_story

    def test_get_nonexistent_story(self, api_client, isolated_story_home):
        resp = api_client.get("/api/story/NONEXIST")
        assert resp.status_code == 404


class TestDiagnosticsAPI:
    def test_debug_endpoint_returns_debug_info(self, api_client, seeded_story):
        resp = api_client.get(f"/api/story/{seeded_story}/debug")
        assert resp.status_code == 200
        data = resp.json()
        assert "story" in data or "recentEvents" in data


class TestSyncAPI:
    def test_sync_status_unconfigured(self, api_client, isolated_story_home):
        resp = api_client.get("/api/sync/tapd/status")
        assert resp.status_code == 200
        assert resp.json()["configured"] is False

    def test_sync_tapd_unconfigured_returns_400(self, api_client, isolated_story_home):
        resp = api_client.post("/api/sync/tapd", json={})
        assert resp.status_code == 400


class TestStoryListWithFilters:
    def test_list_with_overdue_filter(self, api_client, isolated_story_home):
        story1, _ = db.upsert_story_from_source(
            source_type="tapd",
            source_id="1001",
            title="逾期需求",
            deadline="2020-01-01",
        )
        db.update_story(story1["story_key"], intake_state="ready", status="active")
        story2, _ = db.upsert_story_from_source(
            source_type="tapd",
            source_id="1002",
            title="未来需求",
            deadline="2099-12-31",
        )
        db.update_story(story2["story_key"], intake_state="ready", status="active")

        resp = api_client.get("/api/story?overdue=true")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 1
        assert data[0]["title"] == "逾期需求"

    def test_list_returns_new_fields(self, api_client, isolated_story_home):
        story, _ = db.upsert_story_from_source(
            source_type="tapd",
            source_id="1001",
            title="带字段",
            deadline="2026-06-15",
            priority="高",
            tapd_status="open",
        )
        db.update_story(story["story_key"], intake_state="ready", status="active")

        resp = api_client.get("/api/story")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) >= 1
        item = data[0]
        assert item["deadline"] == "2026-06-15"
        assert item["priority"] == "高"
        assert item["tapdStatus"] == "open"

    def test_story_detail_returns_new_fields(self, api_client, isolated_story_home):
        db.upsert_story_from_source(
            source_type="tapd",
            source_id="1001",
            title="详情测试",
            tapd_status="progressing",
        )

        resp = api_client.get("/api/story/tapd-1001")
        assert resp.status_code == 200
        data = resp.json()
        assert data["tapdStatus"] == "progressing"
        assert data["sourceType"] == "tapd"


class TestReleaseTrainAPI:
    """班车看板:release_train 字段 + PUT /release-train 端点。"""

    def test_list_includes_release_train(self, api_client, isolated_story_home):
        db.upsert_story(
            "RT-LIST-1", title="班车列表测试", workspace="/tmp", profile="minimal"
        )
        db.update_story("RT-LIST-1", release_train="v3.2", intake_state="ready")
        resp = api_client.get("/api/story")
        assert resp.status_code == 200
        items = [s for s in resp.json() if s["storyKey"] == "RT-LIST-1"]
        assert items, "story should appear in list"
        assert items[0]["releaseTrain"] == "v3.2"

    def test_detail_includes_release_train(self, api_client, isolated_story_home):
        db.upsert_story(
            "RT-DETAIL-1", title="班车详情测试", workspace="/tmp", profile="minimal"
        )
        db.update_story("RT-DETAIL-1", release_train="后台快线")
        resp = api_client.get("/api/story/RT-DETAIL-1")
        assert resp.status_code == 200
        assert resp.json()["releaseTrain"] == "后台快线"

    def test_set_release_train(self, api_client, isolated_story_home):
        db.upsert_story(
            "RT-SET-1", title="班车拖动测试", workspace="/tmp", profile="minimal"
        )
        resp = api_client.put(
            "/api/story/RT-SET-1/release-train", json={"train": "v3.3"}
        )
        assert resp.status_code == 200
        assert resp.json()["releaseTrain"] == "v3.3"
        assert db.get_story("RT-SET-1")["release_train"] == "v3.3"

    def test_clear_release_train(self, api_client, isolated_story_home):
        db.upsert_story(
            "RT-CLEAR-1", title="班车清空测试", workspace="/tmp", profile="minimal"
        )
        db.update_story("RT-CLEAR-1", release_train="v3.2")
        resp = api_client.put(
            "/api/story/RT-CLEAR-1/release-train", json={"train": None}
        )
        assert resp.status_code == 200
        assert resp.json()["releaseTrain"] is None
        assert db.get_story("RT-CLEAR-1")["release_train"] is None

    def test_set_release_train_logs_event(self, api_client, isolated_story_home):
        db.upsert_story(
            "RT-EVENT-1", title="班车事件测试", workspace="/tmp", profile="minimal"
        )
        api_client.put("/api/story/RT-EVENT-1/release-train", json={"train": "催收线"})
        events = db.get_story_events("RT-EVENT-1")
        rt_events = [e for e in events if e["event_type"] == "release_train_changed"]
        assert len(rt_events) == 1
        payload = db.parse_event_payload(rt_events[0])
        assert payload["to"] == "催收线"

    def test_set_release_train_nonexistent_story(self, api_client, isolated_story_home):
        resp = api_client.put(
            "/api/story/NONEXIST/release-train", json={"train": "v3.2"}
        )
        assert resp.status_code == 404

    def test_empty_string_treated_as_null(self, api_client, isolated_story_home):
        db.upsert_story(
            "RT-EMPTY-1", title="空串归一", workspace="/tmp", profile="minimal"
        )
        db.update_story("RT-EMPTY-1", release_train="v3.2")
        resp = api_client.put(
            "/api/story/RT-EMPTY-1/release-train", json={"train": "  "}
        )
        assert resp.status_code == 200
        assert resp.json()["releaseTrain"] is None
        assert db.get_story("RT-EMPTY-1")["release_train"] is None

    def test_upsert_from_source_does_not_overwrite_release_train(
        self, api_client, isolated_story_home
    ):
        story, _ = db.upsert_story_from_source(
            source_type="tapd",
            source_id="2001",
            title="首次同步",
        )
        db.update_story(story["story_key"], release_train="v3.2", intake_state="ready")
        # 再次同步同一来源,release_train 不应被覆盖
        db.upsert_story_from_source(
            source_type="tapd",
            source_id="2001",
            title="更新标题",
        )
        updated = db.get_story(story["story_key"])
        assert updated["release_train"] == "v3.2"
        assert updated["title"] == "更新标题"


# ---------------------------------------------------------------------------
# design 逐问澄清 HITL API(runbook 块4/块8):GET /clarify + POST /clarify/answer
# ---------------------------------------------------------------------------


class TestInteractiveStagePrompt:
    """交互终端 spawn 时应自动注入 stage prompt(不是空白 ❯ 让人手打)。

    `_ensure_story_agent_pty` 之前传 "" → 起空白 claude。修:复用自主路径的
    _build_cli_prompt 构建 design 提示词注入。人只管 steer,不用手填需求。
    """

    def test_design_prompt_has_title_prd_protocol_donepath(self, isolated_story_home):
        from story_lifecycle.orchestrator.service.api import (
            _build_interactive_stage_prompt,
        )

        db.upsert_story(
            "IP-1",
            title="借款增加第二紧急联系人",
            workspace="/tmp/ip-ws",
            profile="minimal",
            status="planning",
            current_stage="design",
        )
        db.update_context("IP-1", "prd_path", "/tmp/ip-ws/PRD.md")
        story = db.get_story("IP-1")

        p = _build_interactive_stage_prompt(story, "design")

        assert "借款增加第二紧急联系人" in p  # 标题
        assert "/tmp/ip-ws/PRD.md" in p  # PRD 路径注入(让 claude 读)
        assert "mcp__lifecycle__clarify" in p or "设计维度" in p  # design 协议
        assert ".story/done/IP-1/design.json" in p  # done 握手路径

    def test_non_design_stage_still_builds(self, isolated_story_home):
        from story_lifecycle.orchestrator.service.api import (
            _build_interactive_stage_prompt,
        )

        db.upsert_story("IP-2", title="t", workspace="/tmp/ip-ws2", profile="minimal")
        p = _build_interactive_stage_prompt(db.get_story("IP-2"), "build")
        assert isinstance(p, str) and len(p) > 0  # 不抛、非空


def _log_clarify_request(story_key, payload):
    """落 clarification_request 事件(模拟 MCP server 收到 clarify 调用后落的事件)。"""
    db.log_event(story_key, "design", "clarification_request", payload)


class TestClarifyAPI:
    """事件驱动(MCP 方案):request/answer 都走 DB event_log,无侧文件、无重 spawn。"""

    def test_get_clarify_no_pending(self, api_client, isolated_story_home):
        db.upsert_story(
            "CL-1", title="t", workspace="/tmp", profile="minimal", status="active"
        )
        resp = api_client.get("/api/story/CL-1/clarify")
        assert resp.status_code == 200
        assert resp.json()["waiting"] is False

    def test_get_clarify_with_pending(self, api_client, isolated_story_home):
        db.upsert_story(
            "CL-2", title="t", workspace="/tmp", profile="minimal", status="active"
        )
        _log_clarify_request(
            "CL-2",
            {
                "id": "q1",
                "question": "存哪?",
                "header": "存储",
                "options": ["hc_user", "hc_config"],
            },
        )
        resp = api_client.get("/api/story/CL-2/clarify")
        assert resp.status_code == 200
        data = resp.json()
        assert data["waiting"] is True
        assert data["question"]["question"] == "存哪?"
        assert data["question"]["options"] == ["hc_user", "hc_config"]

    def test_answer_writes_event_and_clears_pending(
        self, api_client, isolated_story_home
    ):
        db.upsert_story(
            "CL-3", title="t", workspace="/tmp", profile="minimal", status="active"
        )
        _log_clarify_request(
            "CL-3",
            {"id": "q1", "question": "存哪?", "options": ["hc_user", "hc_config"]},
        )
        resp = api_client.post(
            "/api/story/CL-3/clarify/answer", json={"answer": "hc_user", "id": "q1"}
        )
        assert resp.status_code == 200
        assert resp.json()["answer"] == "hc_user"
        # answer 事件已落 → 该 request 不再 pending
        resp2 = api_client.get("/api/story/CL-3/clarify")
        assert resp2.json()["waiting"] is False

    def test_answer_no_pending_returns_404(self, api_client, isolated_story_home):
        db.upsert_story(
            "CL-4", title="t", workspace="/tmp", profile="minimal", status="active"
        )
        resp = api_client.post("/api/story/CL-4/clarify/answer", json={"answer": "x"})
        assert resp.status_code == 404
