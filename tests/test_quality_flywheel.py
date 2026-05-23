import os
import tempfile
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
