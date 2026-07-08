"""T1.5 · Finding 生命周期(quality 飞轮).

端到端测试 Finding 从 open → accepted → fixed → verified → learned pattern 全链路。
使用 conftest.py 的自动隔离 DB fixture,不污染真实库。
"""

from __future__ import annotations

import json

import pytest

from story_lifecycle.infra.db import models as db
from story_lifecycle.orchestrator.evaluation.quality import (
    activate_pattern,
    approve_pattern,
    propose_learned_pattern,
    record_finding,
    record_verification,
    update_finding_status,
)


@pytest.fixture
def finding_data() -> dict:
    return {
        "source": "code_review",
        "severity": "high",
        "category": "logic",
        "description": "登录接口未校验密码强度",
        "location": "auth.py:42",
        "recommendation": "增加密码复杂度校验",
    }


def test_finding_lifecycle_open_to_learned_pattern(tmp_path, finding_data):
    """Full lifecycle: open -> accepted -> fixed -> verified -> learned pattern."""
    story_key = "STORY-LC-1"
    stage = "verify"

    # 1. record_finding -> status=open, DB has record
    finding_id = record_finding(story_key, stage, finding_data)
    assert finding_id

    finding = db.get_finding(finding_id)
    assert finding is not None
    assert finding["status"] == "open"
    assert finding["description"] == finding_data["description"]

    # 2. update_finding_status -> accepted + audit event
    update_finding_status(story_key, finding_id, "accepted", reason="accepted for fix")
    finding = db.get_finding(finding_id)
    assert finding["status"] == "accepted"

    events = db.get_recent_quality_events(
        story_key, ["finding_status_changed"], limit=1
    )
    assert len(events) == 1
    payload = events[0]["payload"]
    if isinstance(payload, str):
        payload = json.loads(payload)
    assert payload["from"] == "open"
    assert payload["to"] == "accepted"

    # 3. fixed -> verified
    update_finding_status(story_key, finding_id, "fixed", reason="implemented fix")
    finding = db.get_finding(finding_id)
    assert finding["status"] == "fixed"

    record_verification(
        story_key,
        stage,
        commands=[{"cmd": "pytest tests/auth", "status": "passed"}],
        covered_findings=[finding_id],
    )

    update_finding_status(story_key, finding_id, "verified", reason="verified by tests")
    finding = db.get_finding(finding_id)
    assert finding["status"] == "verified"

    # 4. propose_learned_pattern -> status=proposed
    pattern_id = propose_learned_pattern(
        story_key=story_key,
        pattern="密码强度校验缺失",
        applies_to=["auth", "login"],
        rule="所有登录/注册接口必须校验密码复杂度",
        source_findings=[finding_id],
        confidence="high",
    )
    assert pattern_id

    pattern = db.get_learned_pattern(pattern_id)
    assert pattern is not None
    assert pattern["status"] == "proposed"
    assert pattern["pattern"] == "密码强度校验缺失"
    assert finding_id in pattern["source_findings"]

    # 5. approve -> activate -> status=active
    approve_pattern(pattern_id)
    pattern = db.get_learned_pattern(pattern_id)
    assert pattern["status"] == "approved"

    activate_pattern(pattern_id)
    pattern = db.get_learned_pattern(pattern_id)
    assert pattern["status"] == "active"


def test_finding_lifecycle_empty_source_findings_pattern(tmp_path, finding_data):
    """Learned pattern can be proposed without source findings."""
    story_key = "STORY-LC-2"
    stage = "verify"

    pattern_id = propose_learned_pattern(
        story_key=story_key,
        pattern="空 source_findings 也允许",
        applies_to=["general"],
        rule="测试边界",
    )
    assert pattern_id

    pattern = db.get_learned_pattern(pattern_id)
    assert pattern["status"] == "proposed"
    assert pattern["source_findings"] == []
