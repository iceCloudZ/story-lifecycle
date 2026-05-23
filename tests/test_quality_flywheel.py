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
