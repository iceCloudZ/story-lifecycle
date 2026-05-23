"""Tests for Phase 1: Review Feedback Intake Loop."""
import os
import json


def _setup_db(tmp_path):
    """Common DB setup: set STORY_HOME, init_db."""
    os.environ["STORY_HOME"] = str(tmp_path)
    from story_lifecycle.db import models as db
    db.init_db()
    return db


# ── Task 1: DB helpers ──


def test_get_findings_by_status(tmp_path):
    """get_findings_by_status returns findings matching given statuses."""
    db = _setup_db(tmp_path)

    db.create_finding("S1", "impl", "review", "high", "routing", "finding A")
    fid_b = db.create_finding("S1", "impl", "review", "medium", "style", "finding B")
    db.update_finding(fid_b, status="accepted")
    fid_c = db.create_finding("S1", "impl", "review", "low", "style", "finding C")
    db.update_finding(fid_c, status="rejected")

    # open only
    open_f = db.get_findings_by_status(["open"])
    assert len(open_f) == 1
    assert open_f[0]["description"] == "finding A"

    # accepted only
    accepted_f = db.get_findings_by_status(["accepted"])
    assert len(accepted_f) == 1
    assert accepted_f[0]["description"] == "finding B"

    # open + accepted (pending for approval queue)
    pending = db.get_findings_by_status(["open", "accepted"])
    assert len(pending) == 2

    # rejected
    rejected = db.get_findings_by_status(["rejected"])
    assert len(rejected) == 1


def test_get_all_pending_findings(tmp_path):
    """get_all_pending_findings returns open+accepted findings across all stories."""
    db = _setup_db(tmp_path)

    db.create_finding("S1", "impl", "review", "high", "routing", "S1 open")
    fid = db.create_finding("S2", "impl", "review", "medium", "style", "S2 open")
    db.update_finding(fid, status="accepted")
    fid_r = db.create_finding("S3", "impl", "review", "low", "style", "S3 rejected")
    db.update_finding(fid_r, status="rejected")

    pending = db.get_all_pending_findings()
    assert len(pending) == 2
    keys = {f["story_key"] for f in pending}
    assert keys == {"S1", "S2"}


def test_get_findings_by_story(tmp_path):
    """get_findings_by_story returns all findings for a story regardless of status."""
    db = _setup_db(tmp_path)

    db.create_finding("S1", "impl", "review", "high", "routing", "A")
    fid = db.create_finding("S1", "impl", "review", "medium", "style", "B")
    db.update_finding(fid, status="accepted")
    db.create_finding("S2", "impl", "review", "low", "style", "C")

    s1_findings = db.get_findings_by_story("S1")
    assert len(s1_findings) == 2

    s2_findings = db.get_findings_by_story("S2")
    assert len(s2_findings) == 1

    empty = db.get_findings_by_story("S_NONEXIST")
    assert len(empty) == 0