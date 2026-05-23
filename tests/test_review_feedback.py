"""Tests for Phase 1: Review Feedback Intake Loop."""

import json
import os
from unittest.mock import patch, MagicMock


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

    assert result["mode"] == "rule_fallback"
    assert len(result["candidates"]) == 1
    assert result["candidates"][0]["category"] == "security"


def test_extract_candidates_fallback_on_llm_error():
    """LLM error falls back to simple rule-based parser."""
    from story_lifecycle.orchestrator.review_feedback import extract_candidate_findings

    review_md = "- [HIGH] api.py:42 缺少空指针检查\n- [MEDIUM] 缺少测试"
    with patch.dict("os.environ", {"STORY_LLM_API_KEY": "test-key"}):
        with patch("httpx.post", side_effect=Exception("timeout")):
            result = extract_candidate_findings(review_md, "S1")

    assert result["mode"] == "rule_fallback"
    assert len(result["candidates"]) >= 1


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
    # Same category + location should merge — keep higher severity
    assert len(deduped) == 2
    routing = [c for c in deduped if c["category"] == "routing"]
    assert len(routing) == 1
    assert routing[0]["severity"] == "high"


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

    import inspect
    source = inspect.getsource(review_stage)
    assert "只读" in source or "read-only" in source.lower() or "不改代码" in source or "不要修改" in source
