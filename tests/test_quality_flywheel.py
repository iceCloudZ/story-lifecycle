import os

import pytest


def test_finding_lifecycle(tmp_path):
    """Finding should support full lifecycle: open -> accepted -> fixed -> verified -> learned."""
    os.environ["STORY_HOME"] = str(tmp_path)
    from story_lifecycle.db import models as db

    db.init_db()

    fid = db.create_finding(
        story_key="S1", stage="implement", source="code_review",
        severity="high", category="routing",
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
        record_finding, build_quality_packet, build_quality_checklist,
    )

    # No findings → empty packet (just header)
    packet = build_quality_packet("S1", "implement")
    assert "Open Findings: none" in packet

    # Add finding
    record_finding("S1", "implement", {
        "source": "code_review",
        "severity": "high",
        "category": "routing",
        "description": "advance_node missing error path",
        "recommendation": "route last_error to router",
    })

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

    from story_lifecycle.orchestrator.quality import record_finding, update_finding_status

    fid = record_finding("S1", "implement", {
        "source": "code_review",
        "severity": "high",
        "category": "routing",
        "description": "advance_node missing error path",
    })

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

    record_verification("S1", "test", [
        {"cmd": "pytest", "status": "passed"},
        {"cmd": "ruff check", "status": "passed"},
    ], covered_findings=["f1"], commit="abc123")

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

    record_story_intake("S1", "tapd", "1001234", {"has_prd": True, "item_type": "requirement"})

    events = db.get_recent_quality_events("S1", ["story_intake"])
    assert len(events) == 1
    payload = events[0]["payload"]
    import json
    data = json.loads(payload) if isinstance(payload, str) else payload
    assert data["source"] == "tapd"
    assert data["source_id"] == "1001234"
    assert data["has_prd"] is True
