"""Tests for Phase 1: Review Feedback Intake Loop."""

import inspect
import json
import os
from unittest.mock import patch, MagicMock

from click.testing import CliRunner
from fastapi.testclient import TestClient


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


# ── Task 2: Review Feedback Extraction Core ──


def _make_fake_llm_response(json_obj: dict) -> MagicMock:
    mock = MagicMock()
    mock.raise_for_status = MagicMock()
    mock.json.return_value = {
        "choices": [{"message": {"content": json.dumps(json_obj, ensure_ascii=False)}}],
        "usage": {"prompt_tokens": 100, "completion_tokens": 200, "total_tokens": 300},
    }
    return mock


def _make_fake_llm_response_raw(raw: str) -> MagicMock:
    mock = MagicMock()
    mock.raise_for_status = MagicMock()
    mock.json.return_value = {
        "choices": [{"message": {"content": raw}}],
        "usage": {"prompt_tokens": 100, "completion_tokens": 200, "total_tokens": 300},
    }
    return mock


def test_extract_candidates_llm_success():
    """LLM extracts candidate findings from review markdown."""
    from story_lifecycle.orchestrator.review_feedback import extract_candidate_findings

    llm_output = {
        "candidate_findings": [
            {
                "severity": "high",
                "category": "error_handling",
                "description": "缺少空指针检查",
                "location": "api.py:42",
                "recommendation": "添加 null check",
                "root_cause": "接口返回值未校验",
                "evidence": ["api.py:42"],
                "confidence": "high",
            },
            {
                "severity": "medium",
                "category": "missing_test",
                "description": "缺少边界测试",
                "location": "tests/test_api.py",
                "recommendation": "补充边界 case",
                "root_cause": "",
                "evidence": ["tests/test_api.py"],
                "confidence": "medium",
            },
        ],
        "summary": "发现空指针风险和测试缺口",
    }
    fake = _make_fake_llm_response(llm_output)

    review_md = "## Review\n\n### Issues\n- api.py:42 缺少空指针检查\n- 缺少边界测试"
    with patch.dict("os.environ", {"STORY_LLM_API_KEY": "test-key"}):
        with patch("httpx.post", return_value=fake):
            result = extract_candidate_findings(review_md, "S1")

    assert result["mode"] == "llm"
    assert len(result["candidates"]) == 2
    assert result["candidates"][0]["severity"] == "high"
    assert result["candidates"][0]["category"] == "error_handling"


def test_extract_candidates_json_input():
    """Direct JSON input bypasses LLM, parses structured review."""
    from story_lifecycle.orchestrator.review_feedback import extract_candidate_findings

    json_review = json.dumps(
        {
            "findings": [
                {
                    "severity": "high",
                    "category": "security",
                    "description": "SQL injection risk",
                    "location": "dao.py:15",
                    "recommendation": "use parameterized query",
                }
            ]
        }
    )

    with patch.dict("os.environ", {}, clear=True):
        result = extract_candidate_findings(json_review, "S1")

    assert result["mode"] == "llm"
    assert len(result["candidates"]) == 1
    assert result["candidates"][0]["category"] == "security"


def test_extract_candidates_error_on_llm_failure():
    """LLM error returns error mode, no rule fallback."""
    from story_lifecycle.orchestrator.review_feedback import extract_candidate_findings

    review_md = "- [HIGH] api.py:42 缺少空指针检查\n- [MEDIUM] 缺少测试"
    with patch.dict("os.environ", {"STORY_LLM_API_KEY": "test-key"}):
        with patch("httpx.post", side_effect=Exception("timeout")):
            result = extract_candidate_findings(review_md, "S1")

    assert result["mode"] == "error"
    assert len(result["candidates"]) == 0


def test_validate_candidates_rejects_invalid():
    """validate_candidates rejects items missing required fields."""
    from story_lifecycle.orchestrator.review_feedback import validate_candidates

    raw = [
        {"severity": "high", "category": "routing", "description": "valid finding"},
        {"severity": "high", "category": "routing"},  # missing description
        {
            "severity": "critical",
            "category": "routing",
            "description": "bad severity",
        },  # bad severity
    ]
    validated, warnings = validate_candidates(raw)

    assert len(validated) == 2
    assert any("description" in w for w in warnings)
    assert validated[1]["severity"] == "medium"  # default on invalid


def test_dedupe_candidates_merges_similar():
    """dedupe_candidates merges findings with same category+location."""
    from story_lifecycle.orchestrator.review_feedback import dedupe_candidates

    candidates = [
        {
            "severity": "high",
            "category": "routing",
            "description": "路由错误 A",
            "location": "api.py:10",
        },
        {
            "severity": "medium",
            "category": "routing",
            "description": "路由问题，类似A",
            "location": "api.py:10",
        },
        {
            "severity": "low",
            "category": "style",
            "description": "风格问题",
            "location": "utils.py:5",
        },
    ]

    deduped = dedupe_candidates(candidates)
    # Same category+location merged, descriptions concatenated, higher severity kept
    assert len(deduped) == 2
    routing = [c for c in deduped if c["category"] == "routing"]
    assert len(routing) == 1
    assert routing[0]["severity"] == "high"
    assert "路由错误 A" in routing[0]["description"]
    assert "路由问题，类似A" in routing[0]["description"]


def test_dedupe_candidates_against_existing(tmp_path):
    """dedupe_candidates also dedupes against existing DB findings."""
    db = _setup_db(tmp_path)
    from story_lifecycle.orchestrator.review_feedback import dedupe_candidates

    # Existing finding in DB
    db.create_finding(
        "S1",
        "impl",
        "code_review",
        "high",
        "routing",
        "路由错误 A",
        location="api.py:10",
    )

    candidates = [
        {
            "severity": "medium",
            "category": "routing",
            "description": "路由错误 A（重复）",
            "location": "api.py:10",
        },
        {
            "severity": "low",
            "category": "style",
            "description": "新发现",
            "location": "utils.py:5",
        },
    ]

    deduped = dedupe_candidates(candidates, story_key="S1")
    # routing|api.py:10 deduped against DB, style|utils.py:5 kept
    assert len(deduped) == 1
    assert deduped[0]["category"] == "style"


def test_import_review_creates_candidate_findings(tmp_path):
    """import_review writes candidate findings to DB as status=open."""
    db = _setup_db(tmp_path)
    from story_lifecycle.orchestrator.review_feedback import import_review

    llm_output = {
        "candidate_findings": [
            {
                "severity": "high",
                "category": "error_handling",
                "description": "缺少空指针检查",
                "location": "api.py:42",
                "recommendation": "添加 null check",
                "root_cause": "未校验",
                "evidence": ["api.py:42"],
                "confidence": "high",
            },
        ],
        "summary": "test",
    }
    fake = _make_fake_llm_response(llm_output)

    review_md = "## Review\n\n- api.py:42 缺少空指针检查"
    with patch.dict("os.environ", {"STORY_LLM_API_KEY": "test-key"}):
        with patch("httpx.post", return_value=fake):
            result = import_review("S1", review_md)

    assert result["imported"] == 1
    assert len(result["warnings"]) == 0

    # Verify in DB
    findings = db.get_open_findings("S1")
    assert len(findings) == 1
    assert findings[0]["source"] == "review_feedback"
    assert findings[0]["description"] == "缺少空指针检查"


# ── Task 5: Reviewer role guardrail ──


def test_review_prompt_contains_readonly_guardrail():
    """Review prompt in planner.py must contain reviewer read-only constraint."""
    from story_lifecycle.orchestrator.planner import review_stage

    source = inspect.getsource(review_stage)
    assert (
        "只读" in source
        or "read-only" in source.lower()
        or "不改代码" in source
        or "不要修改" in source
    )


# ── Task 3: CLI commands ──


def _write_review_file(tmp_path, content: str) -> str:
    """Write review content to a temp file, return path."""
    f = tmp_path / "review.md"
    f.write_text(content, encoding="utf-8")
    return str(f)


def test_cli_review_feedback_import(tmp_path):
    """story review-feedback import reads file, extracts findings."""
    db = _setup_db(tmp_path)
    runner = CliRunner()

    db.upsert_story("S1", title="Test", workspace=str(tmp_path), current_stage="impl")

    review_content = (
        "## Review\n\n- [HIGH] api.py:42 缺少空指针检查\n- [MEDIUM] 缺少边界测试"
    )
    review_file = _write_review_file(tmp_path, review_content)

    from story_lifecycle.cli.review_feedback import review_feedback_group

    result = runner.invoke(review_feedback_group, ["import", "S1", review_file])

    assert result.exit_code == 0
    assert "finding" in result.output.lower() or "candidate" in result.output.lower()


def test_cli_review_feedback_list(tmp_path):
    """story review-feedback list shows imported findings."""
    db = _setup_db(tmp_path)
    runner = CliRunner()

    db.upsert_story("S1", title="Test", workspace=str(tmp_path), current_stage="impl")
    db.create_finding(
        "S1",
        "review",
        "review_feedback",
        "high",
        "error_handling",
        "空指针检查缺失",
        location="api.py:42",
    )

    from story_lifecycle.cli.review_feedback import review_feedback_group

    result = runner.invoke(review_feedback_group, ["list", "S1"])

    assert result.exit_code == 0
    assert "空指针" in result.output or "error_handling" in result.output


def test_cli_review_feedback_decide_accept(tmp_path):
    """story review-feedback decide --accept changes finding status."""
    db = _setup_db(tmp_path)
    runner = CliRunner()

    db.upsert_story("S1", title="Test", workspace=str(tmp_path), current_stage="impl")
    fid = db.create_finding(
        "S1", "review", "review_feedback", "high", "error_handling", "空指针检查缺失"
    )

    from story_lifecycle.cli.review_feedback import review_feedback_group

    result = runner.invoke(review_feedback_group, ["decide", fid, "--accept"])

    assert result.exit_code == 0
    finding = db.get_finding(fid)
    assert finding["status"] == "accepted"


def test_cli_review_feedback_decide_reject(tmp_path):
    """story review-feedback decide --reject changes finding status."""
    db = _setup_db(tmp_path)
    runner = CliRunner()

    db.upsert_story("S1", title="Test", workspace=str(tmp_path), current_stage="impl")
    fid = db.create_finding(
        "S1", "review", "review_feedback", "high", "error_handling", "空指针检查缺失"
    )

    from story_lifecycle.cli.review_feedback import review_feedback_group

    result = runner.invoke(
        review_feedback_group, ["decide", fid, "--reject", "--reason", "overclaimed"]
    )

    assert result.exit_code == 0
    finding = db.get_finding(fid)
    assert finding["status"] == "rejected"


def test_cli_review_feedback_decide_defer(tmp_path):
    """story review-feedback decide --defer changes finding status."""
    db = _setup_db(tmp_path)
    runner = CliRunner()

    db.upsert_story("S1", title="Test", workspace=str(tmp_path), current_stage="impl")
    fid = db.create_finding(
        "S1", "review", "review_feedback", "low", "style", "格式问题"
    )

    from story_lifecycle.cli.review_feedback import review_feedback_group

    result = runner.invoke(review_feedback_group, ["decide", fid, "--defer"])

    assert result.exit_code == 0
    finding = db.get_finding(fid)
    assert finding["status"] == "deferred"


def test_cli_review_feedback_decide_downgrade(tmp_path):
    """story review-feedback decide --downgrade reduces severity."""
    db = _setup_db(tmp_path)
    runner = CliRunner()

    db.upsert_story("S1", title="Test", workspace=str(tmp_path), current_stage="impl")
    fid = db.create_finding(
        "S1", "review", "review_feedback", "high", "error_handling", "空指针检查缺失"
    )

    from story_lifecycle.cli.review_feedback import review_feedback_group

    result = runner.invoke(review_feedback_group, ["decide", fid, "--downgrade"])

    assert result.exit_code == 0
    finding = db.get_finding(fid)
    assert finding["severity"] == "medium"


def test_cli_approvals_list(tmp_path):
    """story approvals shows pending findings across stories."""
    db = _setup_db(tmp_path)
    runner = CliRunner()

    db.create_finding("S1", "review", "review_feedback", "high", "routing", "S1 issue")
    fid = db.create_finding(
        "S2", "review", "review_feedback", "medium", "style", "S2 issue"
    )
    db.update_finding(fid, status="accepted")

    from story_lifecycle.cli.review_feedback import approvals_group

    result = runner.invoke(approvals_group, ["list"])

    assert result.exit_code == 0
    assert "S1" in result.output
    assert "S2" in result.output


def test_cli_approvals_decide(tmp_path):
    """story approvals decide accepts a finding."""
    db = _setup_db(tmp_path)
    runner = CliRunner()

    fid = db.create_finding(
        "S1", "review", "review_feedback", "high", "routing", "test"
    )

    from story_lifecycle.cli.review_feedback import approvals_group

    result = runner.invoke(approvals_group, ["decide", fid, "--accept"])

    assert result.exit_code == 0
    finding = db.get_finding(fid)
    assert finding["status"] == "accepted"


# ── Task 4: API endpoints ──


def _get_api_client(tmp_path):
    """Create a TestClient with fresh DB."""
    os.environ["STORY_HOME"] = str(tmp_path)
    from story_lifecycle.db.models import init_db

    init_db()
    from story_lifecycle.orchestrator.api import app

    return TestClient(app)


def test_api_import_review_feedback(tmp_path):
    """POST /api/story/{key}/review-feedback imports review content."""
    client = _get_api_client(tmp_path)
    from story_lifecycle.db import models as db

    db.upsert_story("S1", title="Test", workspace=str(tmp_path), current_stage="impl")

    # Use JSON input which works without LLM
    resp = client.post(
        "/api/story/S1/review-feedback",
        json={
            "content": json.dumps(
                {
                    "candidate_findings": [
                        {
                            "severity": "high",
                            "category": "security",
                            "description": "缺少空指针检查",
                            "location": "api.py:42",
                        }
                    ]
                }
            )
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["imported"] >= 1
    assert data["mode"] in ("llm", "error")


def test_api_list_review_feedback(tmp_path):
    """GET /api/story/{key}/review-feedback returns imported findings."""
    client = _get_api_client(tmp_path)
    from story_lifecycle.db import models as db

    db.upsert_story("S1", title="Test", workspace=str(tmp_path), current_stage="impl")
    db.create_finding(
        "S1",
        "review",
        "review_feedback",
        "high",
        "error_handling",
        "test finding",
        location="api.py:42",
    )

    resp = client.get("/api/story/S1/review-feedback")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["findings"]) >= 1
    assert data["findings"][0]["source"] == "review_feedback"


def test_api_decide_finding(tmp_path):
    """PUT /api/finding/{id}/decide updates finding status."""
    client = _get_api_client(tmp_path)
    from story_lifecycle.db import models as db

    db.upsert_story("S1", title="Test", workspace=str(tmp_path), current_stage="impl")
    fid = db.create_finding(
        "S1", "review", "review_feedback", "high", "error_handling", "test finding"
    )

    resp = client.put(
        f"/api/finding/{fid}/decide",
        json={
            "action": "accept",
            "reason": "valid finding",
        },
    )
    assert resp.status_code == 200
    finding = db.get_finding(fid)
    assert finding["status"] == "accepted"


def test_api_decide_finding_reject(tmp_path):
    """PUT /api/finding/{id}/decide rejects finding."""
    client = _get_api_client(tmp_path)
    from story_lifecycle.db import models as db

    db.upsert_story("S1", title="Test", workspace=str(tmp_path), current_stage="impl")
    fid = db.create_finding(
        "S1", "review", "review_feedback", "high", "error_handling", "test finding"
    )

    resp = client.put(
        f"/api/finding/{fid}/decide",
        json={
            "action": "reject",
            "reason": "overclaimed",
        },
    )
    assert resp.status_code == 200
    finding = db.get_finding(fid)
    assert finding["status"] == "rejected"


def test_api_decide_finding_downgrade(tmp_path):
    """PUT /api/finding/{id}/decide downgrades finding severity."""
    client = _get_api_client(tmp_path)
    from story_lifecycle.db import models as db

    db.upsert_story("S1", title="Test", workspace=str(tmp_path), current_stage="impl")
    fid = db.create_finding(
        "S1", "review", "review_feedback", "high", "error_handling", "test finding"
    )

    resp = client.put(
        f"/api/finding/{fid}/decide",
        json={
            "action": "downgrade",
            "reason": "not critical",
        },
    )
    assert resp.status_code == 200
    finding = db.get_finding(fid)
    assert finding["severity"] == "medium"


def test_api_approvals_queue(tmp_path):
    """GET /api/approvals returns pending findings across stories."""
    client = _get_api_client(tmp_path)
    from story_lifecycle.db import models as db

    db.create_finding("S1", "review", "review_feedback", "high", "routing", "S1 issue")
    db.create_finding("S2", "review", "review_feedback", "medium", "style", "S2 issue")
    fid_rejected = db.create_finding(
        "S3", "review", "review_feedback", "low", "style", "S3 rejected"
    )
    db.update_finding(fid_rejected, status="rejected")

    resp = client.get("/api/approvals")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["findings"]) == 2
    stories = {f["story_key"] for f in data["findings"]}
    assert stories == {"S1", "S2"}


def test_api_decide_finding_not_found(tmp_path):
    """PUT /api/finding/{id}/decide returns 404 for missing finding."""
    client = _get_api_client(tmp_path)

    resp = client.put(
        "/api/finding/nonexistent/decide",
        json={
            "action": "accept",
        },
    )
    assert resp.status_code == 404


def test_api_mark_verified_with_evidence(tmp_path):
    """PUT /api/finding/{id}/decide mark_verified writes verification_event_id."""
    client = _get_api_client(tmp_path)
    from story_lifecycle.db import models as db

    db.upsert_story("S1", title="Test", workspace=str(tmp_path), current_stage="impl")
    fid = db.create_finding(
        "S1", "review", "review_feedback", "high", "error_handling", "test finding"
    )

    resp = client.put(
        f"/api/finding/{fid}/decide",
        json={
            "action": "mark_verified",
            "reason": "test passed",
            "verification_event_id": 42,
        },
    )
    assert resp.status_code == 200
    finding = db.get_finding(fid)
    assert finding["status"] == "verified"
    events = db.get_story_events("S1")
    verified_events = []
    for e in events:
        if e.get("event_type") != "finding_status_changed":
            continue
        payload = e.get("payload", "{}")
        if isinstance(payload, str):
            payload = json.loads(payload)
        if "verified" in payload.get("to", ""):
            verified_events.append({"payload": payload})
    assert len(verified_events) == 1
    assert verified_events[0]["payload"]["evidence"]["verification_event_id"] == 42


def test_approvals_decide_accept_single_event(tmp_path):
    """approvals decide --accept writes exactly one status change event."""
    db = _setup_db(tmp_path)

    db.upsert_story("S1", title="Test", workspace=str(tmp_path), current_stage="impl")
    fid = db.create_finding(
        "S1", "review", "review_feedback", "high", "error_handling", "test"
    )

    from click.testing import CliRunner

    runner = CliRunner()
    from story_lifecycle.cli.review_feedback import approvals_group

    result = runner.invoke(approvals_group, ["decide", fid, "--accept"])
    assert result.exit_code == 0

    events = db.get_story_events("S1")
    accept_events = []
    for e in events:
        if e.get("event_type") != "finding_status_changed":
            continue
        payload = e.get("payload", "{}")
        if isinstance(payload, str):
            payload = json.loads(payload)
        if payload.get("to") == "accepted":
            accept_events.append(e)
    assert len(accept_events) == 1, f"Expected 1 accept event, got {len(accept_events)}"


# ── Task 6: E2E integration test ──


def test_review_feedback_intake_e2e(tmp_path):
    """End-to-end: import review -> list -> decide -> verify in quality flywheel."""
    db = _setup_db(tmp_path)
    from story_lifecycle.orchestrator.review_feedback import import_review
    from story_lifecycle.orchestrator.quality import (
        update_finding_status,
        build_quality_packet,
        check_dod,
    )

    # Setup story
    db.upsert_story(
        "TAPD-100100",
        title="逾期利息调整",
        workspace=str(tmp_path),
        current_stage="implement",
    )

    # Verify the story was created
    story = db.get_story("TAPD-100100")
    assert story is not None
    assert story["current_stage"] == "implement"

    # 1. Import review via JSON (structured input, no LLM required)
    review_json = json.dumps(
        {
            "candidate_findings": [
                {
                    "severity": "high",
                    "category": "security",
                    "description": "缺少空指针检查，可能导致 NPE",
                    "location": "api.py:42",
                },
                {
                    "severity": "medium",
                    "category": "testing",
                    "description": "缺少边界测试 case",
                },
                {
                    "severity": "low",
                    "category": "style",
                    "description": "变量命名不规范",
                },
            ]
        }
    )
    env = {"STORY_HOME": str(tmp_path)}
    with patch.dict("os.environ", env):
        result = import_review("TAPD-100100", review_json)

    assert result["imported"] >= 2  # at least the HIGH and MEDIUM
    assert result["mode"] == "llm"

    # 2. List findings
    findings = db.get_open_findings("TAPD-100100")
    assert len(findings) >= 2

    high_f = next(f for f in findings if f["severity"] == "high")
    medium_f = next(f for f in findings if f["severity"] == "medium")

    # 3. DoD should block (open high finding)
    dod = check_dod("TAPD-100100", "implement")
    assert dod["passed"] is False

    # 4. Accept high finding
    update_finding_status("TAPD-100100", high_f["id"], "accepted", reason="valid issue")

    # 5. Reject medium finding
    update_finding_status(
        "TAPD-100100", medium_f["id"], "rejected", reason="style only"
    )

    # 6. Quality packet shows accepted finding
    build_quality_packet("TAPD-100100", "implement")

    # 7. Mark verified
    update_finding_status(
        "TAPD-100100",
        high_f["id"],
        "verified",
        reason="fixed and tested",
        evidence={"verification_event_id": 1},
    )
    finding = db.get_finding(high_f["id"])
    assert finding["status"] == "verified"

    # 8. Audit trail
    events = db.get_story_events("TAPD-100100")
    event_types = {e["event_type"] for e in events}
    assert "review_feedback_imported" in event_types
    assert "finding_status_changed" in event_types

    # 9. DoD should pass now (no open high findings)
    dod2 = check_dod("TAPD-100100", "implement")
    assert dod2["passed"] is True
