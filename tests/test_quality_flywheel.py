import os


def test_finding_lifecycle(tmp_path):
    """Finding should support full lifecycle: open -> accepted -> fixed -> verified -> learned."""
    os.environ["STORY_HOME"] = str(tmp_path)
    from story_lifecycle.db import models as db

    db.init_db()

    fid = db.create_finding(
        story_key="S1",
        stage="implement",
        source="code_review",
        severity="high",
        category="routing",
        description="advance_node missing error path",
        location="nodes.py:747",
        recommendation="route last_error to router",
    )
    assert fid is not None

    # Query open findings
    open_findings = db.get_open_findings("S1")
    assert len(open_findings) == 1
    assert open_findings[0]["status"] == "open"

    # Accept
    db.update_finding(fid, status="accepted")
    assert db.get_finding(fid)["status"] == "accepted"

    # Fix
    db.update_finding(fid, status="fixed")
    assert db.get_finding(fid)["status"] == "fixed"

    # Verify
    db.update_finding(fid, status="verified", verification_event_id=42)
    assert db.get_finding(fid)["status"] == "verified"

    # Learn
    db.update_finding(fid, status="learned")
    assert db.get_finding(fid)["status"] == "learned"

    # No more open findings
    assert len(db.get_open_findings("S1")) == 0


def test_quality_packet_format(tmp_path):
    """Quality Packet should format findings compactly."""
    import os

    os.environ["STORY_HOME"] = str(tmp_path)
    from story_lifecycle.db import models as db

    db.init_db()

    from story_lifecycle.orchestrator.quality import (
        record_finding,
        build_quality_packet,
        build_quality_checklist,
    )

    # No findings → empty packet (just header)
    packet = build_quality_packet("S1", "implement")
    assert "Open Findings: none" in packet

    # Add finding
    record_finding(
        "S1",
        "implement",
        {
            "source": "code_review",
            "severity": "high",
            "category": "routing",
            "description": "advance_node missing error path",
            "recommendation": "route last_error to router",
        },
    )

    packet = build_quality_packet("S1", "implement")
    assert "HIGH" in packet
    assert "routing" in packet
    assert "advance_node missing error path" in packet
    assert "Fix: route last_error to router" in packet

    # Checklist
    checklist = build_quality_checklist("S1", "implement")
    assert "## Quality Checklist" in checklist
    assert "- [ ] Fix: advance_node missing error path" in checklist
    assert "Approach: route last_error to router" in checklist
    assert "pytest && ruff check" in checklist


def test_finding_verification_reopen(tmp_path):
    """Verification failure should reopen a fixed finding."""
    import os

    os.environ["STORY_HOME"] = str(tmp_path)
    from story_lifecycle.db import models as db

    db.init_db()

    from story_lifecycle.orchestrator.quality import (
        record_finding,
        update_finding_status,
    )

    fid = record_finding(
        "S1",
        "implement",
        {
            "source": "code_review",
            "severity": "high",
            "category": "routing",
            "description": "advance_node missing error path",
        },
    )

    # Accept → Fix
    update_finding_status("S1", fid, "accepted")
    update_finding_status("S1", fid, "fixed")

    # Verification fails → reopen
    update_finding_status("S1", fid, "open", reason="verification failed")

    finding = db.get_finding(fid)
    assert finding["status"] == "open"

    # Should appear in open findings again
    open_findings = db.get_open_findings("S1")
    assert len(open_findings) == 1


def test_severity_filtering(tmp_path):
    """get_open_findings should filter by minimum severity."""
    import os

    os.environ["STORY_HOME"] = str(tmp_path)
    from story_lifecycle.db import models as db

    db.init_db()

    db.create_finding("S1", "implement", "review", "high", "routing", "high finding")
    db.create_finding("S1", "implement", "review", "medium", "logic", "medium finding")
    db.create_finding("S1", "implement", "review", "low", "style", "low finding")

    # Default: medium+ only
    findings = db.get_open_findings("S1")
    assert len(findings) == 2
    severities = {f["severity"] for f in findings}
    assert severities == {"high", "medium"}

    # High only
    findings = db.get_open_findings("S1", min_severity="high")
    assert len(findings) == 1
    assert findings[0]["severity"] == "high"


def test_record_verification_event(tmp_path):
    """record_verification should log verification_result event."""
    import os

    os.environ["STORY_HOME"] = str(tmp_path)
    from story_lifecycle.db import models as db

    db.init_db()

    from story_lifecycle.orchestrator.quality import record_verification

    record_verification(
        "S1",
        "test",
        [
            {"cmd": "pytest", "status": "passed"},
            {"cmd": "ruff check", "status": "passed"},
        ],
        covered_findings=["f1"],
        commit="abc123",
    )

    events = db.get_recent_quality_events("S1", ["verification_result"])
    assert len(events) == 1
    payload = events[0]["payload"]
    import json

    data = json.loads(payload) if isinstance(payload, str) else payload
    assert len(data["commands"]) == 2
    assert data["covered_findings"] == ["f1"]
    assert data["commit"] == "abc123"


def test_record_story_intake_event(tmp_path):
    """record_story_intake should log story_intake event."""
    import os

    os.environ["STORY_HOME"] = str(tmp_path)
    from story_lifecycle.db import models as db

    db.init_db()

    from story_lifecycle.orchestrator.quality import record_story_intake

    record_story_intake(
        "S1", "tapd", "1001234", {"has_prd": True, "item_type": "requirement"}
    )

    events = db.get_recent_quality_events("S1", ["story_intake"])
    assert len(events) == 1
    payload = events[0]["payload"]
    import json

    data = json.loads(payload) if isinstance(payload, str) else payload
    assert data["source"] == "tapd"
    assert data["source_id"] == "1001234"
    assert data["has_prd"] is True


def test_learned_pattern_lifecycle(tmp_path):
    """Learned pattern: proposed -> approved -> active -> deprecated."""
    import os

    os.environ["STORY_HOME"] = str(tmp_path)
    from story_lifecycle.db import models as db

    db.init_db()

    # Propose
    pid = db.create_learned_pattern(
        pattern="Graph routing changes require path-level assertions",
        applies_to=["orchestrator.graph", "orchestrator.nodes"],
        rule="Do not assert final status only. Assert event_counts, last_error, retry count.",
        source_findings=["finding-001"],
        confidence="high",
    )
    assert pid is not None
    p = db.get_learned_pattern(pid)
    assert p["status"] == "proposed"
    assert p["applies_to"] == ["orchestrator.graph", "orchestrator.nodes"]
    assert p["source_findings"] == ["finding-001"]

    # Approve
    db.update_learned_pattern(pid, status="approved")
    assert db.get_learned_pattern(pid)["status"] == "approved"

    # Activate
    db.update_learned_pattern(pid, status="active")
    assert db.get_learned_pattern(pid)["status"] == "active"

    # Get active patterns
    active = db.get_active_learned_patterns()
    assert len(active) == 1
    assert active[0]["pattern"] == "Graph routing changes require path-level assertions"

    # Deprecate
    db.update_learned_pattern(pid, status="deprecated")
    assert db.get_learned_pattern(pid)["status"] == "deprecated"
    assert len(db.get_active_learned_patterns()) == 0

    # Reject another pattern
    pid2 = db.create_learned_pattern(
        pattern="Always add logging",
        applies_to=["all-code"],
        rule="Add logging to every function",
        source_findings=[],
        confidence="low",
    )
    db.update_learned_pattern(pid2, status="rejected")
    assert db.get_learned_pattern(pid2)["status"] == "rejected"


def test_find_relevant_patterns(tmp_path):
    """find_relevant_patterns should match by applies_to overlap."""
    import os

    os.environ["STORY_HOME"] = str(tmp_path)
    from story_lifecycle.db import models as db

    db.init_db()

    # Create and activate two patterns
    p1 = db.create_learned_pattern(
        pattern="Graph routing rules",
        applies_to=["orchestrator.graph", "orchestrator.nodes"],
        rule="Assert path behavior",
        source_findings=[],
    )
    db.update_learned_pattern(p1, status="approved")
    db.update_learned_pattern(p1, status="active")

    p2 = db.create_learned_pattern(
        pattern="DB migration rules",
        applies_to=["db.models", "migrations"],
        rule="Always test rollback",
        source_findings=[],
    )
    db.update_learned_pattern(p2, status="approved")
    db.update_learned_pattern(p2, status="active")

    # Search with graph-related tags
    relevant = db.find_relevant_patterns(["orchestrator.nodes", "langgraph"])
    assert len(relevant) == 1
    assert relevant[0]["pattern"] == "Graph routing rules"

    # Search with db tags
    relevant2 = db.find_relevant_patterns(["db.models"])
    assert len(relevant2) == 1
    assert relevant2[0]["pattern"] == "DB migration rules"

    # No match
    relevant3 = db.find_relevant_patterns(["frontend", "react"])
    assert len(relevant3) == 0


def test_learned_pattern_workflow(tmp_path):
    """Full pattern workflow: propose from finding, approve, verify in packet."""
    import os

    os.environ["STORY_HOME"] = str(tmp_path)
    from story_lifecycle.db import models as db

    db.init_db()
    from story_lifecycle.orchestrator.quality import (
        record_finding,
        propose_learned_pattern,
        approve_pattern,
        activate_pattern,
        build_quality_packet,
    )

    # Record finding, mark learned
    fid = record_finding(
        "S1",
        "implement",
        {
            "source": "code_review",
            "severity": "high",
            "category": "routing",
            "description": "advance_node missing error path",
            "recommendation": "route last_error to router",
        },
    )
    db.update_finding(fid, status="learned")

    # Propose pattern from finding
    pid = propose_learned_pattern(
        story_key="S1",
        pattern="Graph routing changes require path-level assertions",
        applies_to=["orchestrator.graph", "orchestrator.nodes"],
        rule="Assert event_counts, last_error, or next_action — not just final status",
        source_findings=[fid],
        confidence="high",
    )
    assert pid is not None

    # Approve + activate
    approve_pattern(pid)
    activate_pattern(pid)

    # Packet for a different story with same tags should include pattern
    packet = build_quality_packet(
        "S2", "implement", relevant_tags=["orchestrator.nodes"]
    )
    assert "Graph routing changes require path-level assertions" in packet
    assert "Assert event_counts" in packet

    # Packet without tags should also show active patterns
    packet2 = build_quality_packet("S3", "implement")
    assert "Graph routing changes require path-level assertions" in packet2


def test_dod_blocks_on_open_high_findings(tmp_path):
    """DoD gate should block when open high findings exist."""
    import os

    os.environ["STORY_HOME"] = str(tmp_path)
    from story_lifecycle.db import models as db

    db.init_db()
    from story_lifecycle.orchestrator.quality import record_finding, check_dod

    record_finding(
        "S1",
        "implement",
        {
            "source": "code_review",
            "severity": "high",
            "category": "routing",
            "description": "advance_node missing error path",
        },
    )

    result = check_dod("S1", "implement")
    assert result["passed"] is False
    assert any("high" in b.lower() for b in result["blocking"])


def test_dod_passes_when_no_high_findings(tmp_path):
    """DoD gate should pass when no high findings exist."""
    import os

    os.environ["STORY_HOME"] = str(tmp_path)
    from story_lifecycle.db import models as db

    db.init_db()
    from story_lifecycle.orchestrator.quality import check_dod

    result = check_dod("S1", "implement")
    assert result["passed"] is True
    assert result["blocking"] == []


def test_dor_check(tmp_path):
    """DoR should check title and source."""
    import os

    os.environ["STORY_HOME"] = str(tmp_path)
    from story_lifecycle.db import models as db

    db.init_db()
    from story_lifecycle.orchestrator.quality import check_dor

    db.upsert_story("S1", title="Test story", workspace="/tmp", current_stage="design")

    result = check_dor("S1", "design")
    assert result["ready"] is True  # title exists

    # Story without title
    db.upsert_story("S2", title="", workspace="/tmp", current_stage="design")
    result2 = check_dor("S2", "design")
    assert result2["ready"] is False
    assert "title" in result2["missing"]


def test_tui_quality_data_queryable(tmp_path):
    """Quality data should be queryable for TUI display."""
    import os

    os.environ["STORY_HOME"] = str(tmp_path)
    from story_lifecycle.db import models as db

    db.init_db()
    from story_lifecycle.orchestrator.quality import record_finding

    record_finding(
        "S1",
        "implement",
        {
            "source": "code_review",
            "severity": "high",
            "category": "routing",
            "description": "advance_node missing error path",
        },
    )

    # Verify data is queryable for TUI
    findings = db.get_open_findings("S1")
    assert len(findings) == 1
    assert findings[0]["severity"] == "high"
    assert findings[0]["category"] == "routing"


def test_api_quality_data_integration(tmp_path):
    """API endpoints should wrap quality data queries."""
    import os

    os.environ["STORY_HOME"] = str(tmp_path)
    from story_lifecycle.db import models as db

    db.init_db()
    from story_lifecycle.orchestrator.quality import record_finding

    record_finding(
        "S1",
        "implement",
        {
            "source": "code_review",
            "severity": "high",
            "category": "routing",
            "description": "advance_node missing error path",
        },
    )

    # Verify DB queryable — API endpoints wrap these
    findings = db.get_open_findings("S1")
    assert len(findings) == 1

    db.log_event("S1", "implement", "readiness_check", {"ready": True})
    events = db.get_recent_quality_events("S1", ["readiness_check"])
    assert len(events) == 1


def test_quality_flywheel_e2e(tmp_path):
    """End-to-end: intake -> finding -> fix -> verify -> learn -> next story gets pattern.

    Simulates the full quality flywheel across two stories:
    - Story S1: code review finds issue, fix it, verify, propose learned pattern
    - Story S2: quality packet includes learned pattern, DoD passes, checklist correct
    """
    import json as _json
    import os

    os.environ["STORY_HOME"] = str(tmp_path)
    from story_lifecycle.db import models as db

    db.init_db()
    from story_lifecycle.orchestrator.quality import (
        record_finding,
        update_finding_status,
        record_verification,
        record_story_intake,
        build_quality_packet,
        build_quality_checklist,
        check_dor,
        check_dod,
        propose_learned_pattern,
        approve_pattern,
        activate_pattern,
    )

    # ================================================================
    # Phase 1: Story S1 — intake
    # ================================================================
    db.upsert_story(
        "TAPD-100100",
        title="逾期利息收取方式调整",
        workspace=str(tmp_path / "ws"),
        profile="minimal",
        current_stage="design",
        status="active",
    )
    db.update_story("TAPD-100100", source_type="tapd", source_id="100100")

    record_story_intake(
        "TAPD-100100",
        "tapd",
        "100100",
        metadata={"has_prd": True, "item_type": "requirement"},
    )
    intake_events = db.get_recent_quality_events("TAPD-100100", ["story_intake"])
    assert len(intake_events) == 1
    intake_data = (
        _json.loads(intake_events[0]["payload"])
        if isinstance(intake_events[0]["payload"], str)
        else intake_events[0]["payload"]
    )
    assert intake_data["source"] == "tapd"

    # DoR check — should pass (has title)
    dor = check_dor("TAPD-100100", "design")
    assert dor["ready"] is True

    # ================================================================
    # Phase 2: S1 design -> implement, code review finds issue
    # ================================================================
    fid1 = record_finding(
        "TAPD-100100",
        "implement",
        {
            "source": "code_review",
            "severity": "high",
            "category": "routing",
            "description": "advance_node missing error path after review",
            "location": "nodes.py:747",
            "recommendation": "route last_error to router node",
            "root_cause": "route_after_advance only checks completed status",
        },
    )
    assert fid1 is not None

    # DoD should BLOCK — open high finding
    dod = check_dod("TAPD-100100", "implement")
    assert dod["passed"] is False
    assert len(dod["blocking"]) == 1

    # Quality packet shows open finding
    packet = build_quality_packet("TAPD-100100", "implement")
    assert "HIGH" in packet
    assert "routing" in packet
    assert "advance_node missing error path" in packet

    # Checklist includes the fix
    checklist = build_quality_checklist("TAPD-100100", "implement")
    assert "Fix: advance_node missing error path" in checklist
    assert "Approach: route last_error to router node" in checklist

    # ================================================================
    # Phase 3: Fix the finding
    # ================================================================
    update_finding_status("TAPD-100100", fid1, "accepted")
    update_finding_status("TAPD-100100", fid1, "fixed")

    # Verify the fix
    record_verification(
        "TAPD-100100",
        "test",
        commands=[
            {"cmd": "pytest", "status": "passed", "summary": "85 passed in 2.3s"},
            {
                "cmd": "ruff check src tests",
                "status": "passed",
                "summary": "All checks passed",
            },
        ],
        covered_findings=[fid1],
        commit="abc1234",
    )
    update_finding_status(
        "TAPD-100100",
        fid1,
        "verified",
        reason="pytest + ruff passed",
        evidence={"verification_event_id": 1},
    )

    # DoD should now PASS — no open high findings
    dod2 = check_dod("TAPD-100100", "implement")
    assert dod2["passed"] is True
    assert dod2["blocking"] == []
    # But has a warning — missing verification (only for story stage check, verify baseline exists)
    verifications = db.get_recent_quality_events("TAPD-100100", ["verification_result"])
    assert len(verifications) == 1

    # ================================================================
    # Phase 4: Mark as learned, propose pattern
    # ================================================================
    update_finding_status("TAPD-100100", fid1, "learned")

    pid1 = propose_learned_pattern(
        story_key="TAPD-100100",
        pattern="Graph routing changes require path-level assertions",
        applies_to=["orchestrator.graph", "orchestrator.nodes"],
        rule="Do not assert final status only. Assert event_counts, last_error, or next_action.",
        source_findings=[fid1],
        confidence="high",
    )
    assert pid1 is not None

    # Human approval workflow
    approve_pattern(pid1)
    activate_pattern(pid1)

    # Pattern is now active
    active = db.get_active_learned_patterns()
    assert len(active) == 1
    assert active[0]["status"] == "active"

    # ================================================================
    # Phase 5: Story S2 — new story should see learned pattern
    # ================================================================
    db.upsert_story(
        "TAPD-100200",
        title="还款方式优化",
        workspace=str(tmp_path / "ws2"),
        profile="minimal",
        current_stage="design",
        status="active",
    )
    db.update_story("TAPD-100200", source_type="tapd", source_id="100200")

    record_story_intake(
        "TAPD-100200",
        "tapd",
        "100200",
        metadata={"has_prd": False, "item_type": "requirement"},
    )

    # DoR for S2
    dor_s2 = check_dor("TAPD-100200", "design")
    assert dor_s2["ready"] is True  # has title
    assert "no PRD file" in dor_s2["warnings"]  # but no PRD

    # Quality packet for S2 with orchestrator.nodes tags — should include learned pattern
    packet_s2 = build_quality_packet(
        "TAPD-100200",
        "implement",
        relevant_tags=["orchestrator.nodes", "orchestrator.graph"],
    )
    assert "Graph routing changes require path-level assertions" in packet_s2
    assert "Do not assert final status only" in packet_s2
    # S2 has no findings, so open findings section says none
    assert "Open Findings: none" in packet_s2

    # DoD for S2 — no findings, should pass
    dod_s2 = check_dod("TAPD-100200", "implement")
    assert dod_s2["passed"] is True

    # ================================================================
    # Phase 6: S2 review finds a recurrence of the same pattern
    # ================================================================
    fid2 = record_finding(
        "TAPD-100200",
        "implement",
        {
            "source": "code_review",
            "severity": "high",
            "category": "routing",
            "description": "router_node only checks last_error, missing retry count assertion",
            "location": "nodes.py:820",
            "recommendation": "assert retry count in addition to error message",
        },
    )

    # DoD should block again
    dod_s2_blocked = check_dod("TAPD-100200", "implement")
    assert dod_s2_blocked["passed"] is False

    # Fix + verify
    update_finding_status("TAPD-100200", fid2, "accepted")
    update_finding_status("TAPD-100200", fid2, "fixed")
    record_verification(
        "TAPD-100200",
        "test",
        commands=[
            {"cmd": "pytest", "status": "passed", "summary": "92 passed in 2.8s"},
            {"cmd": "ruff check src tests", "status": "passed"},
        ],
        covered_findings=[fid2],
        commit="def5678",
    )
    update_finding_status(
        "TAPD-100200",
        fid2,
        "verified",
        reason="all tests pass",
        evidence={"verification_event_id": 2},
    )
    update_finding_status("TAPD-100200", fid2, "learned")

    # Propose a refined pattern
    pid2 = propose_learned_pattern(
        story_key="TAPD-100200",
        pattern="Router assertions must include retry count and error message",
        applies_to=["orchestrator.graph", "orchestrator.nodes", "orchestrator.router"],
        rule="When testing router_node, assert retry_count > 0 AND last_error is set.",
        source_findings=[fid1, fid2],
        confidence="high",
    )
    approve_pattern(pid2)
    activate_pattern(pid2)

    # ================================================================
    # Phase 7: Verify final state
    # ================================================================
    # Two active patterns
    all_active = db.get_active_learned_patterns()
    assert len(all_active) == 2

    # Relevance search — both match orchestrator.nodes
    relevant = db.find_relevant_patterns(["orchestrator.nodes"])
    assert len(relevant) == 2

    # Relevance search — only one matches orchestrator.router
    router_only = db.find_relevant_patterns(["orchestrator.router"])
    assert len(router_only) == 1
    assert (
        router_only[0]["pattern"]
        == "Router assertions must include retry count and error message"
    )

    # S1 has no open findings (all learned)
    assert len(db.get_open_findings("TAPD-100100")) == 0

    # S2 has no open findings (all learned)
    assert len(db.get_open_findings("TAPD-100200")) == 0

    # Audit trail — check key events exist
    all_s1_events = db.get_story_events("TAPD-100100")
    event_types = {e["event_type"] for e in all_s1_events}
    assert "story_intake" in event_types
    assert "code_review_finding" in event_types
    assert "finding_status_changed" in event_types
    assert "verification_result" in event_types
    assert "learned_pattern" in event_types
    assert "readiness_check" in event_types

    # Final quality packet for a hypothetical S3 with all patterns
    packet_s3 = build_quality_packet(
        "TAPD-100300", "implement", relevant_tags=["orchestrator.graph"]
    )
    assert "Graph routing changes require path-level assertions" in packet_s3
    assert "Router assertions must include retry count" in packet_s3


def test_derive_relevance_tags(tmp_path):
    """_derive_relevance_tags should extract tags from story context and DB."""
    import os

    os.environ["STORY_HOME"] = str(tmp_path)
    from story_lifecycle.db import models as db

    db.init_db()
    from story_lifecycle.orchestrator.nodes import _derive_relevance_tags

    # Create story with source_type and sub_type in DB
    db.upsert_story(
        "S1",
        title="Test story",
        workspace=str(tmp_path),
        current_stage="design",
    )
    db.update_story("S1", source_type="tapd", sub_type="requirement")

    state = {
        "story_key": "S1",
        "current_stage": "design",
        "profile": "minimal",
        "context": {
            "affected_modules": ["orchestrator.graph", "orchestrator.nodes"],
            "touched_paths": ["orchestrator/nodes.py", "orchestrator/graph.py"],
            "category": "routing",
        },
    }

    tags = _derive_relevance_tags(state, "design")

    assert "design" in tags
    assert "orchestrator.graph" in tags
    assert "orchestrator.nodes" in tags
    assert "orchestrator" in tags  # extracted from touched_paths
    assert "routing" in tags
    assert "minimal" in tags
    assert "tapd" in tags
    assert "requirement" in tags


def test_derive_relevance_tags_handles_string_modules(tmp_path):
    """_derive_relevance_tags should handle affected_modules as a single string."""
    import os

    os.environ["STORY_HOME"] = str(tmp_path)
    from story_lifecycle.db import models as db

    db.init_db()
    from story_lifecycle.orchestrator.nodes import _derive_relevance_tags

    db.upsert_story(
        "S2", title="test", workspace=str(tmp_path), current_stage="implement"
    )

    state = {
        "story_key": "S2",
        "current_stage": "implement",
        "context": {"affected_modules": "db.models"},
    }

    tags = _derive_relevance_tags(state, "implement")
    assert "db.models" in tags


def test_build_quality_packet_relevance_filtering(tmp_path):
    """build_quality_packet with relevant_tags should filter patterns, without should show all."""
    import os

    os.environ["STORY_HOME"] = str(tmp_path)
    from story_lifecycle.db import models as db

    db.init_db()
    from story_lifecycle.orchestrator.quality import (
        build_quality_packet,
        propose_learned_pattern,
        approve_pattern,
        activate_pattern,
    )

    # Create two patterns for different domains
    p1 = propose_learned_pattern(
        "S1",
        pattern="Graph routing rules",
        applies_to=["orchestrator.graph"],
        rule="Assert path behavior",
        confidence="high",
    )
    approve_pattern(p1)
    activate_pattern(p1)

    p2 = propose_learned_pattern(
        "S1",
        pattern="DB migration rules",
        applies_to=["db.models"],
        rule="Test rollback",
        confidence="high",
    )
    approve_pattern(p2)
    activate_pattern(p2)

    # Without tags → all active patterns shown
    packet_all = build_quality_packet("S1", "implement")
    assert "Graph routing rules" in packet_all
    assert "DB migration rules" in packet_all

    # With orchestrator tags → only graph pattern shown
    packet_filtered = build_quality_packet(
        "S1", "implement", relevant_tags=["orchestrator.graph"]
    )
    assert "Graph routing rules" in packet_filtered
    assert "DB migration rules" not in packet_filtered

    # With db tags → only db pattern shown
    packet_db = build_quality_packet("S1", "implement", relevant_tags=["db.models"])
    assert "DB migration rules" in packet_db
    assert "Graph routing rules" not in packet_db

    # No overlap → no patterns
    packet_none = build_quality_packet("S1", "implement", relevant_tags=["frontend"])
    assert "Relevant Learned Patterns:" not in packet_none


# -------- seed pipeline tests --------


def test_load_manifest_valid():
    """load_manifest should accept valid YAML manifests."""
    from story_lifecycle.orchestrator.seed_pipeline import load_manifest

    manifest = load_manifest(
        {
            "story_key": "STORY-001",
            "title": "Test Story",
            "type": "requirement",
            "source_root": "/tmp",
            "artifacts": [
                {"path": "prd/001.md", "type": "prd"},
            ],
            "known_outcomes": ["outcome 1"],
        }
    )
    assert manifest["story_key"] == "STORY-001"
    assert manifest["type"] == "requirement"
    assert len(manifest["artifacts"]) == 1


def test_load_manifest_rejects_missing_story_key():
    """load_manifest should reject manifests without story_key."""
    from story_lifecycle.orchestrator.seed_pipeline import load_manifest

    try:
        load_manifest({"title": "Test", "type": "requirement", "artifacts": []})
        assert False, "Expected ValueError"
    except ValueError as e:
        assert "story_key" in str(e)


def test_load_manifest_rejects_empty_artifacts():
    """load_manifest should reject manifests with empty artifacts."""
    from story_lifecycle.orchestrator.seed_pipeline import load_manifest

    try:
        load_manifest(
            {
                "story_key": "S1",
                "title": "Test",
                "type": "requirement",
                "artifacts": [],
            }
        )
        assert False, "Expected ValueError"
    except ValueError as e:
        assert "artifacts" in str(e)


def test_load_manifest_rejects_invalid_type():
    """load_manifest should reject invalid story types."""
    from story_lifecycle.orchestrator.seed_pipeline import load_manifest

    try:
        load_manifest(
            {
                "story_key": "S1",
                "title": "Test",
                "type": "unknown_type",
                "artifacts": [{"path": "x.md", "type": "prd"}],
            }
        )
        assert False, "Expected ValueError"
    except ValueError as e:
        assert "type" in str(e)


def test_load_manifest_rejects_invalid_artifact_type():
    """load_manifest should reject unknown artifact types."""
    from story_lifecycle.orchestrator.seed_pipeline import load_manifest

    try:
        load_manifest(
            {
                "story_key": "S1",
                "title": "Test",
                "type": "requirement",
                "artifacts": [{"path": "x.md", "type": "video"}],
            }
        )
        assert False, "Expected ValueError"
    except ValueError as e:
        assert "type" in str(e) or "video" in str(e)


def test_artifact_loader_missing_files(tmp_path):
    """load_artifacts should raise FileNotFoundError listing all missing files."""
    from story_lifecycle.orchestrator.seed_pipeline import load_artifacts

    manifest = {
        "story_key": "S1",
        "source_root": str(tmp_path),
        "artifacts": [
            {"path": "missing1.md", "type": "prd"},
            {"path": "missing2.md", "type": "plan"},
        ],
    }

    try:
        load_artifacts(manifest)
        assert False, "Expected FileNotFoundError"
    except FileNotFoundError as e:
        msg = str(e)
        assert "missing1.md" in msg
        assert "missing2.md" in msg


def test_artifact_loader_truncation(tmp_path):
    """load_artifacts should truncate large files and set truncated=True."""
    from story_lifecycle.orchestrator.seed_pipeline import load_artifacts

    big_file = tmp_path / "big.md"
    big_file.write_text("x" * 25000, encoding="utf-8")

    manifest = {
        "story_key": "S1",
        "source_root": str(tmp_path),
        "artifacts": [{"path": "big.md", "type": "prd"}],
    }

    artifacts = load_artifacts(manifest)
    assert len(artifacts) == 1
    assert artifacts[0]["truncated"] is True
    assert len(artifacts[0]["content"]) <= 20_000


def test_artifact_loader_empty_file(tmp_path):
    """load_artifacts should handle empty files."""
    from story_lifecycle.orchestrator.seed_pipeline import load_artifacts

    (tmp_path / "empty.md").write_text("", encoding="utf-8")

    manifest = {
        "story_key": "S1",
        "source_root": str(tmp_path),
        "artifacts": [{"path": "empty.md", "type": "prd"}],
    }

    artifacts = load_artifacts(manifest)
    assert len(artifacts) == 1
    assert "(empty file)" in artifacts[0]["content"]


def test_artifact_loader_absolute_path(tmp_path):
    """load_artifacts should resolve absolute paths regardless of source_root."""
    from story_lifecycle.orchestrator.seed_pipeline import load_artifacts

    abs_file = tmp_path / "absolute.md"
    abs_file.write_text("absolute content", encoding="utf-8")

    manifest = {
        "story_key": "S1",
        "source_root": "/nonexistent",
        "artifacts": [{"path": str(abs_file), "type": "prd"}],
    }

    artifacts = load_artifacts(manifest)
    assert len(artifacts) == 1
    assert "absolute content" in artifacts[0]["content"]


def test_context_summarizer(tmp_path):
    """summarize_context should produce compact text from artifacts."""
    from story_lifecycle.orchestrator.seed_pipeline import summarize_context

    artifacts = [
        {
            "path": "prd/001.md",
            "type": "prd",
            "content": "# PRD\n\n验收标准: xyz\n\n详细内容\n" * 10,
            "truncated": False,
        },
        {
            "path": "plan/001.md",
            "type": "plan",
            "content": "实现路径: abc\n风险: 低",
            "truncated": False,
        },
    ]
    manifest = {"known_outcomes": ["outcome A"]}

    result = summarize_context(artifacts, manifest)
    assert "prd" in result
    assert "验收标准" in result
    assert "实现路径" in result
    assert "outcome A" in result
    assert "---" in result  # section separator


def test_schema_validator_rejects_broad_applies_to():
    """validate_proposal should reject patterns with all-broad applies_to."""
    from story_lifecycle.orchestrator.seed_pipeline import validate_proposal

    llm_output = {
        "story_key": "S1",
        "summary": "test",
        "risk_tags": [],
        "proposed_findings": [],
        "proposed_patterns": [
            {
                "pattern": "Test pattern",
                "applies_to": ["backend"],
                "rule": "Some rule",
                "evidence": ["x.md"],
                "confidence": "medium",
            }
        ],
        "review_questions": [],
    }

    validated, warnings = validate_proposal(llm_output, {"story_key": "S1"})
    assert len(validated["proposed_patterns"]) == 0
    assert any("broad" in w.lower() or "rejected" in w.lower() for w in warnings)


def test_schema_validator_accepts_narrow_tags():
    """validate_proposal should accept patterns with at least one non-broad tag."""
    from story_lifecycle.orchestrator.seed_pipeline import validate_proposal

    llm_output = {
        "story_key": "S1",
        "summary": "test",
        "risk_tags": [],
        "proposed_findings": [],
        "proposed_patterns": [
            {
                "pattern": "Test pattern",
                "applies_to": ["backend", "hc-order"],
                "rule": "Some rule",
                "evidence": ["x.md"],
                "confidence": "medium",
            }
        ],
        "review_questions": [],
    }

    validated, _warnings = validate_proposal(llm_output, {"story_key": "S1"})
    assert len(validated["proposed_patterns"]) == 1


def test_schema_validator_rejects_missing_evidence():
    """validate_proposal should reject findings without evidence."""
    from story_lifecycle.orchestrator.seed_pipeline import validate_proposal

    llm_output = {
        "story_key": "S1",
        "summary": "test",
        "risk_tags": [],
        "proposed_findings": [
            {
                "severity": "medium",
                "category": "routing",
                "description": "Missing evidence finding",
                "evidence": [],
                "confidence": "medium",
            }
        ],
        "proposed_patterns": [],
        "review_questions": [],
    }

    validated, warnings = validate_proposal(llm_output, {"story_key": "S1"})
    assert len(validated["proposed_findings"]) == 0
    assert any("evidence" in w.lower() for w in warnings)


def test_schema_validator_count_limits():
    """validate_proposal should enforce max findings/patterns limits."""
    from story_lifecycle.orchestrator.seed_pipeline import validate_proposal

    many_findings = []
    for i in range(7):
        many_findings.append(
            {
                "severity": "medium",
                "category": "routing",
                "description": f"Finding {i}",
                "evidence": [f"file{i}.md"],
                "confidence": "medium",
            }
        )

    validated, warnings = validate_proposal(
        {
            "story_key": "S1",
            "proposed_findings": many_findings,
            "proposed_patterns": [],
        },
        {"story_key": "S1"},
    )
    assert len(validated["proposed_findings"]) == 5
    assert any(
        "truncated" in w.lower() or "beyond limit" in w.lower() for w in warnings
    )


def test_schema_validator_invalid_severity():
    """validate_proposal should default invalid severity to 'medium'."""
    from story_lifecycle.orchestrator.seed_pipeline import validate_proposal

    llm_output = {
        "story_key": "S1",
        "summary": "test",
        "risk_tags": [],
        "proposed_findings": [
            {
                "severity": "critical",
                "category": "routing",
                "description": "Some finding",
                "evidence": ["x.md"],
                "confidence": "medium",
            }
        ],
        "proposed_patterns": [],
        "review_questions": [],
    }

    validated, warnings = validate_proposal(llm_output, {"story_key": "S1"})
    assert validated["proposed_findings"][0]["severity"] == "medium"
    assert any("severity" in w.lower() for w in warnings)


def test_schema_validator_empty_proposals():
    """validate_proposal should accept empty findings/patterns lists."""
    from story_lifecycle.orchestrator.seed_pipeline import validate_proposal

    llm_output = {
        "story_key": "S1",
        "summary": "",
        "risk_tags": [],
        "proposed_findings": [],
        "proposed_patterns": [],
        "review_questions": [],
    }

    validated, warnings = validate_proposal(llm_output, {"story_key": "S1"})
    assert validated["proposed_findings"] == []
    assert validated["proposed_patterns"] == []
    assert len(warnings) == 0


def test_schema_validator_story_key_mismatch():
    """validate_proposal should warn and correct story_key mismatch."""
    from story_lifecycle.orchestrator.seed_pipeline import validate_proposal

    llm_output = {
        "story_key": "WRONG-KEY",
        "summary": "",
        "risk_tags": [],
        "proposed_findings": [],
        "proposed_patterns": [],
        "review_questions": [],
    }

    validated, warnings = validate_proposal(llm_output, {"story_key": "RIGHT-KEY"})
    assert validated["story_key"] == "RIGHT-KEY"
    assert any("mismatch" in w.lower() for w in warnings)


def test_write_and_load_proposal_roundtrip(tmp_path):
    """write_proposal + load_reviewed_proposal should roundtrip correctly."""
    import json

    from story_lifecycle.orchestrator.seed_pipeline import (
        write_proposal,
        load_reviewed_proposal,
    )

    proposal = {
        "story_key": "S1",
        "summary": "test summary",
        "risk_tags": ["tag1"],
        "proposed_findings": [],
        "proposed_patterns": [],
        "review_questions": ["Q1?"],
    }
    manifest = {
        "story_key": "S1",
        "title": "Test",
        "type": "requirement",
        "source_root": str(tmp_path),
        "artifacts": [{"path": "x.md", "type": "prd"}],
    }

    filepath = write_proposal(proposal, manifest, str(tmp_path), dry_run=False)
    assert filepath is not None
    assert filepath.exists()

    # Before review: should fail
    try:
        load_reviewed_proposal(str(filepath))
        assert False, "Expected ValueError"
    except ValueError as e:
        assert "not been reviewed" in str(e)

    # After review: should succeed
    doc = json.loads(filepath.read_text(encoding="utf-8"))
    doc["review_status"]["reviewed_at"] = "2026-05-23T00:00:00Z"
    doc["review_status"]["findings_approved"] = [0]
    filepath.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")

    loaded = load_reviewed_proposal(str(filepath))
    assert loaded["manifest"]["story_key"] == "S1"
    assert loaded["review_status"]["reviewed_at"] is not None


def test_dry_run_does_not_write(tmp_path):
    """write_proposal with dry_run=True should return None and not write files."""
    from story_lifecycle.orchestrator.seed_pipeline import write_proposal

    proposal = {
        "story_key": "S1",
        "summary": "",
        "risk_tags": [],
        "proposed_findings": [],
        "proposed_patterns": [],
        "review_questions": [],
    }
    manifest = {
        "story_key": "S1",
        "title": "Test",
        "type": "requirement",
        "source_root": str(tmp_path),
        "artifacts": [],
    }

    result = write_proposal(proposal, manifest, str(tmp_path), dry_run=True)
    assert result is None
    # No proposals dir should have been created
    proposals_dir = tmp_path / ".story/quality-seed/proposals"
    assert not proposals_dir.exists()


def test_apply_writes_to_db(tmp_path):
    """apply_reviewed should write approved findings/patterns to DB."""
    import os

    os.environ["STORY_HOME"] = str(tmp_path)
    from story_lifecycle.db import models as db

    db.init_db()
    from story_lifecycle.orchestrator.seed_pipeline import apply_reviewed

    proposal = {
        "manifest": {"story_key": "S1"},
        "review_status": {
            "findings_approved": [0],
            "patterns_approved": [0],
            "findings_rejected": [1],
            "patterns_rejected": [],
            "reviewed_at": "2026-05-23T00:00:00Z",
        },
        "proposed_findings": [
            {
                "severity": "high",
                "category": "routing",
                "description": "Approved finding",
                "evidence": ["x.md"],
                "confidence": "medium",
                "location": "nodes.py",
                "root_cause": "test",
                "recommendation": "fix it",
            },
            {
                "severity": "low",
                "category": "style",
                "description": "Rejected finding",
                "evidence": ["y.md"],
                "confidence": "low",
            },
        ],
        "proposed_patterns": [
            {
                "pattern": "Approved pattern",
                "applies_to": ["orchestrator", "nodes"],
                "rule": "Do X when Y",
                "evidence": ["x.md"],
                "confidence": "medium",
            },
        ],
    }

    result = apply_reviewed(proposal)
    assert result["findings_written"] == 1
    assert result["patterns_written"] == 1
    assert result["errors"] == []

    # Verify finding was written
    open_findings = db.get_open_findings("S1")
    assert len(open_findings) == 1
    assert open_findings[0]["description"] == "Approved finding"

    # Verify pattern was written as proposed
    proposed = db.get_proposed_learned_patterns()
    assert len(proposed) == 1
    assert proposed[0]["pattern"] == "Approved pattern"
    assert proposed[0]["status"] == "proposed"

    # Verify pattern is NOT active
    active = db.get_active_learned_patterns()
    assert len(active) == 0


def test_apply_only_writes_approved_indices(tmp_path):
    """apply_reviewed should only write items at approved index positions."""
    import os

    os.environ["STORY_HOME"] = str(tmp_path)
    from story_lifecycle.db import models as db

    db.init_db()
    from story_lifecycle.orchestrator.seed_pipeline import apply_reviewed

    proposal = {
        "manifest": {"story_key": "S2"},
        "review_status": {
            "findings_approved": [0],  # Only first of three
            "patterns_approved": [],
            "findings_rejected": [1, 2],
            "patterns_rejected": [],
            "reviewed_at": "2026-05-23T00:00:00Z",
        },
        "proposed_findings": [
            {
                "severity": "high",
                "category": "routing",
                "description": "F1",
                "evidence": ["a.md"],
                "confidence": "high",
            },
            {
                "severity": "medium",
                "category": "style",
                "description": "F2",
                "evidence": ["b.md"],
                "confidence": "medium",
            },
            {
                "severity": "low",
                "category": "style",
                "description": "F3",
                "evidence": ["c.md"],
                "confidence": "low",
            },
        ],
        "proposed_patterns": [],
    }

    result = apply_reviewed(proposal)
    assert result["findings_written"] == 1

    open_findings = db.get_open_findings("S2")
    assert len(open_findings) == 1
    assert open_findings[0]["description"] == "F1"


def test_apply_handles_out_of_range_index(tmp_path):
    """apply_reviewed should report error for out-of-range approved indices."""
    import os

    os.environ["STORY_HOME"] = str(tmp_path)
    from story_lifecycle.db import models as db

    db.init_db()
    from story_lifecycle.orchestrator.seed_pipeline import apply_reviewed

    proposal = {
        "manifest": {"story_key": "S3"},
        "review_status": {
            "findings_approved": [99],
            "patterns_approved": [],
            "findings_rejected": [],
            "patterns_rejected": [],
            "reviewed_at": "2026-05-23T00:00:00Z",
        },
        "proposed_findings": [
            {
                "severity": "high",
                "category": "routing",
                "description": "F1",
                "evidence": ["a.md"],
                "confidence": "high",
            },
        ],
        "proposed_patterns": [],
    }

    result = apply_reviewed(proposal)
    assert result["findings_written"] == 0
    assert len(result["errors"]) == 1
    assert "99" in result["errors"][0]
