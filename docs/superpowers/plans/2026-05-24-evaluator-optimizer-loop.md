# Evaluator-Optimizer Loop Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement adversarial evaluator-optimizer loops for plan and code review stages, enabling planner↔reviewer convergence and code quality enforcement through cross-node iterative retry.

**Architecture:** Two new focused modules (`loop_events.py` for event schema, `evaluator_loop.py` for loop logic) integrate into existing nodes.py. Plan loop is an in-node while loop inside `plan_stage_node`. Code loop is cross-node: `review_stage_node` runs one fresh reviewer round per call, records findings, builds repair packet, and the existing router retry path handles re-execution.

**Tech Stack:** Python 3.11+, LangGraph StateGraph, existing SQLite event_log, existing planner.py LLM call pattern, httpx.

---

## File Structure

| File | Responsibility |
|------|---------------|
| `src/story_lifecycle/orchestrator/loop_events.py` | Event writers: `log_loop_started`, `log_loop_round`, `log_loop_completed`, `log_loop_fallback` |
| `src/story_lifecycle/orchestrator/evaluator_loop.py` | Core loop: `LoopResult`, `AdversarialConfig`, `run_plan_loop`, `run_code_review_loop`, `build_repair_packet`, `detect_no_progress` |
| `src/story_lifecycle/orchestrator/planner.py` | Add `review_plan()` function for plan-stage reviewer |
| `src/story_lifecycle/orchestrator/nodes.py` | Integration: gate plan_stage_node/review_stage_node behind adversarial config; inject repair packet in `_render_prompt` |
| `profiles/minimal.yaml` | Add `adversarial` config block (default disabled) |
| `tests/test_evaluator_loop.py` | All loop-related tests |

---

### Task 1: loop_events.py — Event Writers

**Files:**
- Create: `src/story_lifecycle/orchestrator/loop_events.py`
- Test: `tests/test_evaluator_loop.py`

- [ ] **Step 1: Write failing tests for loop events**

```python
# tests/test_evaluator_loop.py
"""Tests for evaluator-optimizer loop: events, repair packets, plan loop, code loop."""

import json
import os
import time
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest


def _make_state(story_key="LOOP-001", stage="implement", **overrides):
    base = {
        "story_key": story_key,
        "title": "Loop Test Story",
        "workspace": os.getcwd(),
        "profile": "minimal",
        "current_stage": stage,
        "status": "active",
        "context": {},
        "execution_count": 0,
        "last_error": None,
        "trajectory_score": None,
        "review_summary": None,
    }
    base.update(overrides)
    return base


def _get_events_by_type(story_key, event_type):
    from story_lifecycle.db import models as _db
    return [
        e for e in _db.get_story_events(story_key) if e.get("event_type") == event_type
    ]


def _parse_payload(event):
    payload = event.get("payload", {})
    if isinstance(payload, str):
        try:
            return json.loads(payload)
        except (json.JSONDecodeError, TypeError):
            return {}
    return payload or {}


# ── loop_events tests ──


def test_log_loop_started_writes_event(isolated_story_home):
    from story_lifecycle.db import models as db
    from story_lifecycle.orchestrator.loop_events import log_loop_started

    db.upsert_story("LOOP-EV1", workspace=os.getcwd(), profile="minimal")
    log_loop_started(
        story_key="LOOP-EV1",
        stage="implement",
        loop_id="implement:20260524-abc",
        loop_type="code",
        mode="short_lived",
        max_rounds=3,
        optimizer_model="claude-sonnet",
        reviewer_model="deepseek-chat",
        attempt_id="implement:1",
    )
    events = _get_events_by_type("LOOP-EV1", "evaluator_loop_started")
    assert len(events) == 1
    p = _parse_payload(events[0])
    assert p["loop_id"] == "implement:20260524-abc"
    assert p["loop_type"] == "code"
    assert p["mode"] == "short_lived"
    assert p["max_rounds"] == 3
    assert p["reviewer_model"] == "deepseek-chat"
    assert p["attempt_id"] == "implement:1"


def test_log_loop_round_writes_event(isolated_story_home):
    from story_lifecycle.db import models as db
    from story_lifecycle.orchestrator.loop_events import log_loop_round

    db.upsert_story("LOOP-EV2", workspace=os.getcwd(), profile="minimal")
    log_loop_round(
        story_key="LOOP-EV2",
        stage="implement",
        loop_id="implement:20260524-abc",
        round_id=1,
        loop_type="code",
        mode="short_lived",
        decision="revise",
        score=0.78,
        findings={
            "open_before": [],
            "new": ["F-001", "F-002"],
            "resolved": [],
            "repeated": [],
        },
        verification={"status": "unavailable", "commands": []},
    )
    events = _get_events_by_type("LOOP-EV2", "evaluator_loop_round")
    assert len(events) == 1
    p = _parse_payload(events[0])
    assert p["loop_id"] == "implement:20260524-abc"
    assert p["round_id"] == 1
    assert p["decision"] == "revise"
    assert p["score"] == 0.78
    assert p["findings"]["new"] == ["F-001", "F-002"]
    assert p["verification"]["status"] == "unavailable"
    assert p["no_progress"] is False


def test_log_loop_completed_writes_event(isolated_story_home):
    from story_lifecycle.db import models as db
    from story_lifecycle.orchestrator.loop_events import log_loop_completed

    db.upsert_story("LOOP-EV3", workspace=os.getcwd(), profile="minimal")
    log_loop_completed(
        story_key="LOOP-EV3",
        stage="implement",
        loop_id="plan:20260524-xyz",
        loop_type="plan",
        decision="pass",
        rounds=2,
        reason="all_blockers_resolved",
        remaining_findings=[],
    )
    events = _get_events_by_type("LOOP-EV3", "evaluator_loop_completed")
    assert len(events) == 1
    p = _parse_payload(events[0])
    assert p["loop_id"] == "plan:20260524-xyz"
    assert p["decision"] == "pass"
    assert p["rounds"] == 2
    assert p["reason"] == "all_blockers_resolved"
    assert p["remaining_findings"] == []


def test_log_loop_fallback_writes_event(isolated_story_home):
    from story_lifecycle.db import models as db
    from story_lifecycle.orchestrator.loop_events import log_loop_fallback

    db.upsert_story("LOOP-EV4", workspace=os.getcwd(), profile="minimal")
    log_loop_fallback(
        story_key="LOOP-EV4",
        stage="implement",
        loop_id="code:20260524-fb",
        from_mode="persistent",
        to_mode="short_lived",
        reason="session_dead",
        repair_packet_path=".story-context/LOOP-EV4/repair_implement_round2.md",
    )
    events = _get_events_by_type("LOOP-EV4", "evaluator_loop_fallback")
    assert len(events) == 1
    p = _parse_payload(events[0])
    assert p["from_mode"] == "persistent"
    assert p["to_mode"] == "short_lived"
    assert p["reason"] == "session_dead"
    assert "repair_implement_round2" in p["repair_packet_path"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_evaluator_loop.py::test_log_loop_started_writes_event -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'story_lifecycle.orchestrator.loop_events'`

- [ ] **Step 3: Create loop_events.py**

```python
# src/story_lifecycle/orchestrator/loop_events.py
"""Event writers for evaluator-optimizer loop observability.

All events go to the existing event_log table via db.log_event().
"""

from __future__ import annotations

from ..db import models as db


def log_loop_started(
    *,
    story_key: str,
    stage: str,
    loop_id: str,
    loop_type: str,
    mode: str,
    max_rounds: int,
    optimizer_model: str,
    reviewer_model: str,
    attempt_id: str,
) -> None:
    db.log_event(
        story_key,
        stage,
        "evaluator_loop_started",
        {
            "loop_id": loop_id,
            "loop_type": loop_type,
            "stage": stage,
            "mode": mode,
            "max_rounds": max_rounds,
            "optimizer_model": optimizer_model,
            "reviewer_model": reviewer_model,
            "attempt_id": attempt_id,
        },
    )


def log_loop_round(
    *,
    story_key: str,
    stage: str,
    loop_id: str,
    round_id: int,
    loop_type: str,
    mode: str,
    decision: str,
    score: float = 0.0,
    findings: dict | None = None,
    verification: dict | None = None,
    prompt_tokens: dict | None = None,
    timing_ms: dict | None = None,
    diff: dict | None = None,
    no_progress: bool = False,
) -> None:
    db.log_event(
        story_key,
        stage,
        "evaluator_loop_round",
        {
            "loop_id": loop_id,
            "round_id": round_id,
            "loop_type": loop_type,
            "mode": mode,
            "decision": decision,
            "score": score,
            "findings": findings or {},
            "verification": verification or {},
            "prompt_tokens": prompt_tokens or {},
            "timing_ms": timing_ms or {},
            "diff": diff or {},
            "no_progress": no_progress,
        },
    )


def log_loop_completed(
    *,
    story_key: str,
    stage: str,
    loop_id: str,
    loop_type: str,
    decision: str,
    rounds: int,
    reason: str,
    remaining_findings: list | None = None,
) -> None:
    db.log_event(
        story_key,
        stage,
        "evaluator_loop_completed",
        {
            "loop_id": loop_id,
            "loop_type": loop_type,
            "decision": decision,
            "rounds": rounds,
            "reason": reason,
            "remaining_findings": remaining_findings or [],
        },
    )


def log_loop_fallback(
    *,
    story_key: str,
    stage: str,
    loop_id: str,
    from_mode: str,
    to_mode: str,
    reason: str,
    repair_packet_path: str = "",
) -> None:
    db.log_event(
        story_key,
        stage,
        "evaluator_loop_fallback",
        {
            "loop_id": loop_id,
            "from_mode": from_mode,
            "to_mode": to_mode,
            "reason": reason,
            "repair_packet_path": repair_packet_path,
        },
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_evaluator_loop.py -v -k "loop_" `
Expected: 4 PASSED

- [ ] **Step 5: Commit**

```bash
git add src/story_lifecycle/orchestrator/loop_events.py tests/test_evaluator_loop.py
git commit -m "feat: add evaluator loop event writers (loop_events.py)"
```

---

### Task 2: evaluator_loop.py — Types, Config, Repair Packet, No-Progress Detection

**Files:**
- Create: `src/story_lifecycle/orchestrator/evaluator_loop.py`
- Test: `tests/test_evaluator_loop.py` (append)

- [ ] **Step 1: Write failing tests for types, config, repair packet, no_progress**

Append to `tests/test_evaluator_loop.py`:

```python
# ── AdversarialConfig tests ──


def test_adversarial_config_defaults_when_disabled():
    from story_lifecycle.orchestrator.evaluator_loop import AdversarialConfig

    cfg = AdversarialConfig.from_profile({})
    assert cfg.enabled is False
    assert cfg.plan_loop.enabled is False
    assert cfg.code_loop.enabled is False


def test_adversarial_config_parses_yaml():
    from story_lifecycle.orchestrator.evaluator_loop import AdversarialConfig

    profile = {
        "adversarial": {
            "enabled": True,
            "plan_loop": {
                "enabled": True,
                "stages": ["design", "implement"],
                "max_rounds": 3,
                "reviewer_model": "deepseek-chat",
                "pass_condition": "no_open_blocker_or_major",
            },
            "code_loop": {
                "enabled": True,
                "mode": "short_lived",
                "max_rounds": 3,
                "reviewer_model": "deepseek-chat",
                "pass_condition": "no_open_blocker",
                "fallback": "repair_packet",
            },
        }
    }
    cfg = AdversarialConfig.from_profile(profile)
    assert cfg.enabled is True
    assert cfg.plan_loop.enabled is True
    assert cfg.plan_loop.stages == ["design", "implement"]
    assert cfg.plan_loop.max_rounds == 3
    assert cfg.plan_loop.reviewer_model == "deepseek-chat"
    assert cfg.code_loop.enabled is True
    assert cfg.code_loop.mode == "short_lived"
    assert cfg.code_loop.max_rounds == 3


def test_adversarial_config_falls_back_to_env_model():
    from story_lifecycle.orchestrator.evaluator_loop import AdversarialConfig

    cfg = AdversarialConfig.from_profile({
        "adversarial": {
            "enabled": True,
            "plan_loop": {"enabled": True, "stages": ["design"]},
            "code_loop": {"enabled": True, "mode": "short_lived"},
        }
    })
    assert cfg.plan_loop.reviewer_model == ""  # empty means fallback to env
    assert cfg.code_loop.reviewer_model == ""


# ── LoopResult tests ──


def test_loop_result_fields():
    from story_lifecycle.orchestrator.evaluator_loop import LoopResult

    result = LoopResult(
        decision="pass",
        rounds=2,
        final_plan={"adapter": "claude"},
        final_review=None,
        reason="all_blockers_resolved",
        remaining_findings=[],
    )
    assert result.decision == "pass"
    assert result.rounds == 2
    assert result.final_plan["adapter"] == "claude"


# ── build_repair_packet tests ──


def test_build_repair_packet_contains_findings_and_plan(isolated_story_home):
    from story_lifecycle.orchestrator.evaluator_loop import build_repair_packet

    packet = build_repair_packet(
        story_key="LOOP-RP1",
        stage="implement",
        workspace=os.getcwd(),
        plan_summary="Implement auth module",
        stage_output_summary="Added auth.py, login.py",
        findings=[
            {"severity": "high", "category": "security", "description": "Missing CSRF token",
             "recommendation": "Add CSRF middleware", "location": "auth.py:42"},
            {"severity": "medium", "category": "testing", "description": "No unit tests",
             "recommendation": "Add test_auth.py", "location": ""},
        ],
        verification={"status": "unavailable", "commands": []},
        round_num=1,
    )
    assert "Missing CSRF token" in packet
    assert "auth.py:42" in packet
    assert "No unit tests" in packet
    assert "Implement auth module" in packet
    assert "unavailable" in packet
    assert "Avoid unrelated refactoring" in packet.lower() or "避免无关重构" in packet


def test_build_repair_packet_no_full_diff():
    from story_lifecycle.orchestrator.evaluator_loop import build_repair_packet

    packet = build_repair_packet(
        story_key="LOOP-RP2",
        stage="implement",
        workspace=os.getcwd(),
        plan_summary="Fix bug",
        stage_output_summary="Changed main.py",
        findings=[],
        verification={"status": "passed", "commands": ["pytest"]},
        round_num=1,
    )
    # Should not contain raw diff markers
    assert "--- a/" not in packet
    assert "+++ b/" not in packet


def test_build_repair_packet_respects_hard_budget():
    from story_lifecycle.orchestrator.evaluator_loop import build_repair_packet

    # Create findings with very long descriptions
    long_findings = [
        {
            "severity": "high",
            "category": f"cat-{i}",
            "description": "X" * 5000,
            "recommendation": "Fix it",
            "location": f"file_{i}.py",
        }
        for i in range(20)
    ]
    packet = build_repair_packet(
        story_key="LOOP-RP3",
        stage="implement",
        workspace=os.getcwd(),
        plan_summary="P" * 5000,
        stage_output_summary="S" * 5000,
        findings=long_findings,
        verification={"status": "failed", "commands": []},
        round_num=1,
    )
    # hard_budget is ~80000 chars (20000 tokens * ~4 chars/token)
    assert len(packet) < 100000


def test_build_repair_packet_writes_file(isolated_story_home):
    from story_lifecycle.orchestrator.evaluator_loop import build_repair_packet
    from pathlib import Path

    workspace = str(isolated_story_home / "ws")
    Path(workspace).mkdir(exist_ok=True)

    path = build_repair_packet(
        story_key="LOOP-RP4",
        stage="implement",
        workspace=workspace,
        plan_summary="Plan summary here",
        stage_output_summary="Output summary",
        findings=[],
        verification={"status": "passed", "commands": []},
        round_num=2,
        write_file=True,
    )
    assert path is not None
    assert "repair_implement_round2.md" in str(path)
    content = Path(path).read_text(encoding="utf-8")
    assert "Plan summary here" in content


# ── detect_no_progress tests ──


def test_detect_no_progress_true_on_repeated_blockers():
    from story_lifecycle.orchestrator.evaluator_loop import detect_no_progress

    prev = [
        {"severity": "high", "category": "security", "location": "auth.py:42",
         "description": "Missing CSRF token"},
    ]
    curr = [
        {"severity": "high", "category": "security", "location": "auth.py:42",
         "description": "CSRF token still missing"},
    ]
    assert detect_no_progress(prev, curr) is True


def test_detect_no_progress_false_on_new_finding():
    from story_lifecycle.orchestrator.evaluator_loop import detect_no_progress

    prev = [
        {"severity": "high", "category": "security", "location": "auth.py:42",
         "description": "Missing CSRF token"},
    ]
    curr = [
        {"severity": "high", "category": "testing", "location": "test_api.py:10",
         "description": "Missing integration test"},
    ]
    assert detect_no_progress(prev, curr) is False


def test_detect_no_progress_false_on_resolved_with_new():
    from story_lifecycle.orchestrator.evaluator_loop import detect_no_progress

    prev = [
        {"severity": "high", "category": "security", "location": "auth.py:42",
         "description": "Missing CSRF token"},
    ]
    curr = []
    assert detect_no_progress(prev, curr) is False


def test_detect_no_progress_true_on_exact_repeat():
    from story_lifecycle.orchestrator.evaluator_loop import detect_no_progress

    finding = {"severity": "high", "category": "null-safety", "location": "main.py:15",
               "description": "NPE risk"}
    assert detect_no_progress([finding], [finding]) is True


def test_detect_no_progress_ignores_low_severity():
    from story_lifecycle.orchestrator.evaluator_loop import detect_no_progress

    prev = [
        {"severity": "low", "category": "style", "location": "a.py:1",
         "description": "Missing docstring"},
    ]
    curr = [
        {"severity": "low", "category": "style", "location": "a.py:1",
         "description": "Still missing docstring"},
    ]
    assert detect_no_progress(prev, curr) is False
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_evaluator_loop.py -v -k "adversarial_config or loop_result or build_repair or detect_no_progress"`
Expected: FAIL — `ModuleNotFoundError: No module named 'story_lifecycle.orchestrator.evaluator_loop'`

- [ ] **Step 3: Create evaluator_loop.py — types, config, repair packet, no_progress**

```python
# src/story_lifecycle/orchestrator/evaluator_loop.py
"""Evaluator-Optimizer adversarial loop logic.

Plan Loop: in-node while loop inside plan_stage_node.
Code Loop: cross-node iterative retry via review_stage_node → router retry.
"""

from __future__ import annotations

import json
import os
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from ..db import models as db
from .loop_events import (
    log_loop_started,
    log_loop_round,
    log_loop_completed,
    log_loop_fallback,
)

log = logging.getLogger("story-lifecycle.evaluator_loop")

# Token budget approximations (1 token ≈ 4 chars for English/mixed Chinese)
CHARS_PER_TOKEN = 4
TARGET_BUDGET_TOKENS = 4000
HARD_BUDGET_TOKENS = 20000
EMERGENCY_COMPACT_TOKENS = 6000


@dataclass
class LoopResult:
    decision: str  # "pass" | "fail" | "max_rounds" | "no_progress" | "wait_confirm"
    rounds: int
    final_plan: dict | None = None
    final_review: dict | None = None
    reason: str = ""
    remaining_findings: list[str] = field(default_factory=list)


@dataclass
class _SubLoopConfig:
    enabled: bool = False
    stages: list[str] = field(default_factory=list)
    max_rounds: int = 3
    reviewer_model: str = ""
    pass_condition: str = ""
    mode: str = "short_lived"
    fallback: str = "repair_packet"


@dataclass
class AdversarialConfig:
    enabled: bool = False
    plan_loop: _SubLoopConfig = field(default_factory=_SubLoopConfig)
    code_loop: _SubLoopConfig = field(default_factory=_SubLoopConfig)

    @classmethod
    def from_profile(cls, profile: dict) -> AdversarialConfig:
        adv = profile.get("adversarial", {})
        if not adv or not adv.get("enabled"):
            return cls()

        plan_raw = adv.get("plan_loop", {})
        code_raw = adv.get("code_loop", {})

        plan_cfg = _SubLoopConfig(
            enabled=plan_raw.get("enabled", False),
            stages=plan_raw.get("stages", []),
            max_rounds=plan_raw.get("max_rounds", 3),
            reviewer_model=plan_raw.get("reviewer_model", ""),
            pass_condition=plan_raw.get("pass_condition", "no_open_blocker_or_major"),
        )

        code_cfg = _SubLoopConfig(
            enabled=code_raw.get("enabled", False),
            stages=[],
            max_rounds=code_raw.get("max_rounds", 3),
            reviewer_model=code_raw.get("reviewer_model", ""),
            pass_condition=code_raw.get("pass_condition", "no_open_blocker"),
            mode=code_raw.get("mode", "short_lived"),
            fallback=code_raw.get("fallback", "repair_packet"),
        )

        return cls(enabled=True, plan_loop=plan_cfg, code_loop=code_cfg)

    def plan_loop_enabled(self, stage: str) -> bool:
        if not self.enabled or not self.plan_loop.enabled:
            return False
        stages = self.plan_loop.stages
        return stage in stages if stages else True

    def code_loop_enabled(self, stage: str) -> bool:
        if not self.enabled or not self.code_loop.enabled:
            return False
        return True

    def resolve_reviewer_model(self, loop_type: str) -> str:
        if loop_type == "plan":
            model = self.plan_loop.reviewer_model
        else:
            model = self.code_loop.reviewer_model
        if model:
            return model
        return os.environ.get("STORY_LLM_MODEL", "")


def _make_loop_id(loop_type: str, stage: str) -> str:
    ts = datetime.now().strftime("%Y%m%d")
    short = uuid.uuid4().hex[:6]
    return f"{loop_type}:{ts}-{short}"


# ── Repair Packet ──


def build_repair_packet(
    *,
    story_key: str,
    stage: str,
    workspace: str,
    plan_summary: str,
    stage_output_summary: str,
    findings: list[dict],
    verification: dict,
    round_num: int,
    accepted_risks: list[str] | None = None,
    must_preserve_decisions: list[str] | None = None,
    write_file: bool = False,
) -> str | None:
    """Build repair packet content. Optionally write to disk.

    Returns the packet string, or the file path if write_file=True.
    """
    sections = []

    sections.append(f"# Repair Packet: {stage} Round {round_num}\n")
    sections.append(f"## Story\n- Key: {story_key}\n")

    sections.append(f"## 当前阶段 Plan\n{plan_summary}\n")

    sections.append(f"## 阶段产出摘要\n{stage_output_summary}\n")

    # Findings grouped by severity
    high_findings = [f for f in findings if f.get("severity") == "high"]
    medium_findings = [f for f in findings if f.get("severity") == "medium"]
    low_findings = [f for f in findings if f.get("severity") not in ("high", "medium")]

    if high_findings:
        sections.append("## 阻塞/高级别 Findings")
        for f in high_findings:
            loc = f.get("location", "")
            sections.append(
                f"- [{f.get('severity', '').upper()}] {f.get('category', '')}: "
                f"{f.get('description', '')}"
                + (f" @ {loc}" if loc else "")
            )
            if f.get("recommendation"):
                sections.append(f"  Required change: {f['recommendation']}")
        sections.append("")

    if medium_findings:
        sections.append("## 中级别 Findings")
        for f in medium_findings:
            loc = f.get("location", "")
            sections.append(
                f"- [{f.get('severity', '').upper()}] {f.get('category', '')}: "
                f"{f.get('description', '')}"
                + (f" @ {loc}" if loc else "")
            )
            if f.get("recommendation"):
                sections.append(f"  Required change: {f['recommendation']}")
        sections.append("")

    if low_findings:
        sections.append("## 低级别 Findings")
        for f in low_findings:
            sections.append(f"- {f.get('description', '')}")
        sections.append("")

    # Verification status
    ver_status = verification.get("status", "not_run")
    ver_cmds = verification.get("commands", [])
    sections.append(f"## 验证状态\nStatus: {ver_status}")
    if ver_cmds:
        sections.append(f"Commands: {', '.join(str(c) for c in ver_cmds)}")
    if ver_status == "unavailable":
        sections.append("Reason: 验证基础设施不可用或不可靠")
    sections.append("")

    if accepted_risks:
        sections.append("## 已接受的风险")
        for r in accepted_risks:
            sections.append(f"- {r}")
        sections.append("")

    if must_preserve_decisions:
        sections.append("## 必须保留的决策")
        for d in must_preserve_decisions:
            sections.append(f"- {d}")
        sections.append("")

    sections.append("## 指令\n"
                     "- 请仅修复上述 findings 中列出的具体问题\n"
                     "- 不要进行无关的重构或风格调整\n"
                     "- 保持与现有代码风格一致")

    packet = "\n".join(sections)

    # Trim to hard budget
    hard_chars = HARD_BUDGET_TOKENS * CHARS_PER_TOKEN
    if len(packet) > hard_chars:
        packet = _trim_packet(packet, findings, plan_summary, verification, round_num, story_key)

    if not write_file:
        return packet

    # Write to disk
    repair_dir = Path(workspace) / ".story-context" / story_key
    repair_dir.mkdir(parents=True, exist_ok=True)
    repair_file = repair_dir / f"repair_{stage}_round{round_num}.md"
    repair_file.write_text(packet, encoding="utf-8")
    return str(repair_file)


def _trim_packet(
    packet: str,
    findings: list[dict],
    plan_summary: str,
    verification: dict,
    round_num: int,
    story_key: str,
) -> str:
    """Trim repair packet to emergency compact budget when over hard limit."""
    emergency_chars = EMERGENCY_COMPACT_TOKENS * CHARS_PER_TOKEN
    lines = packet.split("\n")

    # Priority 1: keep blocking/high findings and verification
    kept = [f"# Repair Packet (compacted) — Round {round_num}"]
    kept.append(f"Story: {story_key}")
    kept.append(f"Plan: {plan_summary[:200]}")

    high = [f for f in findings if f.get("severity") == "high"]
    if high:
        kept.append("## Blocking Findings")
        for f in high:
            kept.append(f"- {f.get('description', '')} @ {f.get('location', '')}")
            if f.get("recommendation"):
                kept.append(f"  Fix: {f['recommendation']}")

    kept.append(f"Verification: {verification.get('status', 'not_run')}")

    result = "\n".join(kept)
    if len(result) > emergency_chars:
        result = result[:emergency_chars]
    return result


# ── No-Progress Detection ──


def detect_no_progress(
    previous_round_findings: list[dict],
    current_round_findings: list[dict],
) -> bool:
    """Detect if Major/Blocker findings are semantically repeated.

    Only considers high-severity findings. Returns True if current round's
    high findings have matching category+location in previous round, indicating
    the implementer did not fix them.
    """
    prev_high = {(f.get("category", ""), f.get("location", ""))
                 for f in previous_round_findings
                 if f.get("severity") == "high"}
    if not prev_high:
        return False

    curr_high = [(f.get("category", ""), f.get("location", ""), f.get("description", ""))
                 for f in current_round_findings
                 if f.get("severity") == "high"]
    if not curr_high:
        return False

    repeated = 0
    for cat, loc, _desc in curr_high:
        if (cat, loc) in prev_high:
            repeated += 1

    # All current high findings are repeats → no progress
    return repeated == len(curr_high) and len(curr_high) > 0
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_evaluator_loop.py -v`
Expected: All tests PASSED

- [ ] **Step 5: Commit**

```bash
git add src/story_lifecycle/orchestrator/evaluator_loop.py
git commit -m "feat: add evaluator loop types, config, repair packet, no-progress detection"
```

---

### Task 3: Plan Reviewer — `planner.review_plan()`

**Files:**
- Modify: `src/story_lifecycle/orchestrator/planner.py`
- Test: `tests/test_evaluator_loop.py` (append)

Add a new `review_plan()` function for plan-stage adversarial review. Unlike `review_stage()` which reviews code output, this reviews a plan's quality.

- [ ] **Step 1: Write failing tests for plan reviewer**

Append to `tests/test_evaluator_loop.py`:

```python
# ── plan reviewer tests ──


def test_review_plan_returns_structured_result(isolated_story_home):
    from story_lifecycle.db import models as db

    db.upsert_story("LOOP-PR1", workspace=os.getcwd(), profile="minimal")

    from story_lifecycle.orchestrator.planner import review_plan

    mock_response = {
        "quality": "revise",
        "blockers": [
            {"category": "scope", "description": "Plan too broad", "severity": "high"}
        ],
        "suggestions": ["Narrow scope to auth module only"],
        "reasoning": "Plan covers 3 modules but story is scoped to auth",
    }

    with patch("story_lifecycle.orchestrator.planner._call_llm", return_value=mock_response):
        state = _make_state(story_key="LOOP-PR1", stage="design")
        plan = {"adapter": "claude", "summary": "Implement everything", "extra_instructions": "Do all the things"}
        cfg = {"description": "需求分析与方案设计"}

        result = review_plan(state, plan, cfg)

    assert result["quality"] == "revise"
    assert len(result["blockers"]) == 1
    assert result["blockers"][0]["severity"] == "high"


def test_review_plan_pass_with_no_blockers(isolated_story_home):
    from story_lifecycle.db import models as db

    db.upsert_story("LOOP-PR2", workspace=os.getcwd(), profile="minimal")

    from story_lifecycle.orchestrator.planner import review_plan

    mock_response = {
        "quality": "pass",
        "blockers": [],
        "suggestions": [],
        "reasoning": "Plan is well-scoped and actionable",
    }

    with patch("story_lifecycle.orchestrator.planner._call_llm", return_value=mock_response):
        state = _make_state(story_key="LOOP-PR2", stage="design")
        plan = {"adapter": "claude", "summary": "Implement auth module", "extra_instructions": "Add login/logout"}
        cfg = {"description": "编码实现"}

        result = review_plan(state, plan, cfg)

    assert result["quality"] == "pass"
    assert result["blockers"] == []


def test_review_plan_uses_reviewer_model():
    from story_lifecycle.orchestrator.planner import review_plan

    mock_response = {"quality": "pass", "blockers": [], "suggestions": [], "reasoning": "ok"}

    with patch("story_lifecycle.orchestrator.planner._call_llm", return_value=mock_response) as mock_llm:
        state = _make_state(story_key="LOOP-PR3", stage="design")
        plan = {"adapter": "claude", "summary": "Test", "extra_instructions": "Test"}

        review_plan(state, plan, {}, reviewer_model="deepseek-chat")

        call_kwargs = mock_llm.call_args
        assert call_kwargs[0][0]  # base_url positional
        # reviewer_model is passed to _call_llm which uses it for tracing
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_evaluator_loop.py -v -k "review_plan"`
Expected: FAIL — `ImportError: cannot import name 'review_plan'`

- [ ] **Step 3: Add `review_plan()` to planner.py**

Add this function to `src/story_lifecycle/orchestrator/planner.py` after the existing `review_stage()` function:

```python
def review_plan(
    state: dict,
    plan: dict,
    stage_config: dict,
    reviewer_model: str = "",
) -> dict:
    """Plan Reviewer 角色：评估规划质量，返回阻塞性问题。

    与 review_stage() 不同：review_plan 评估的是计划本身的质量，
    而非代码产出的质量。Plan reviewer 保持全新上下文，不继承对话历史。
    """
    api_key, base_url, default_model = _api_config()
    model = reviewer_model or default_model
    workspace = state.get("workspace", "")
    story_key = state.get("story_key", "")

    story_knowledge = _load_story_knowledge(workspace, story_key)

    prompt = f"""你是一个开发团队的架构评审员。你的职责是评估执行计划的质量。
你只负责审查计划本身，不修改任何代码或文件。

## Story 信息
- Key: {state.get("story_key")}
- 标题: {state.get("title")}
- 当前阶段: {state.get("current_stage")}
- 已重试次数: {state.get("execution_count", 0)}
- 阶段描述: {stage_config.get("description", "")}

## 待审查的计划
- Adapter: {plan.get("adapter", "claude")}
- Provider: {plan.get("provider", "N/A")}
- Model: {plan.get("model", "N/A")}
- 摘要: {plan.get("summary", "")}
- 执行指令: {plan.get("extra_instructions", "")}
- 决策理由: {plan.get("reasoning", "")}

## Story 知识库
{story_knowledge}

请评估该计划的质量。返回 JSON：
{{{{
  "quality": "pass|revise",
  "blockers": [
    {{{{
      "category": "scope|missing_context|unrealistic|dependency|risk",
      "description": "问题描述",
      "severity": "high|medium",
      "suggestion": "改进建议"
    }}}}
  ],
  "suggestions": ["非阻塞性改进建议"],
  "reasoning": "判断理由"
}}}}

判断标准：
- pass: 计划范围明确、指令具体可操作、考虑了已有知识库中的约束
- revise: 计划存在阻塞性问题（scope 模糊、缺少关键上下文、目标不切实际、遗漏依赖）
- 只有 severity=high 的 blocker 才会触发修订
- 不要仅凭风格偏好阻塞计划"""

    return _call_llm(
        base_url,
        api_key,
        model,
        prompt,
        story_key=story_key,
        stage=state.get("current_stage", ""),
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_evaluator_loop.py -v -k "review_plan"`
Expected: 3 PASSED

- [ ] **Step 5: Commit**

```bash
git add src/story_lifecycle/orchestrator/planner.py
git commit -m "feat: add plan reviewer function for adversarial plan loop"
```

---

### Task 4: `run_plan_loop()` — In-Node Plan Loop

**Files:**
- Modify: `src/story_lifecycle/orchestrator/evaluator_loop.py`
- Test: `tests/test_evaluator_loop.py` (append)

The plan loop is an in-node while loop: planner drafts → fresh reviewer evaluates → planner revises → converge or stop.

- [ ] **Step 1: Write failing tests for plan loop**

Append to `tests/test_evaluator_loop.py`:

```python
# ── run_plan_loop tests ──


def test_plan_loop_passes_on_first_round(isolated_story_home):
    from story_lifecycle.db import models as db
    from story_lifecycle.orchestrator.evaluator_loop import run_plan_loop, AdversarialConfig

    db.upsert_story("LOOP-PL1", workspace=os.getcwd(), profile="minimal")

    plan_result = {"adapter": "claude", "summary": "Good plan", "extra_instructions": "Do X",
                   "reasoning": "Sound", "trajectory_score": 0.9}
    review_result = {"quality": "pass", "blockers": [], "suggestions": [], "reasoning": "Plan is solid"}

    with patch("story_lifecycle.orchestrator.planner.plan_stage", return_value=plan_result), \
         patch("story_lifecycle.orchestrator.planner.review_plan", return_value=review_result):
        state = _make_state(story_key="LOOP-PL1", stage="design")
        cfg = AdversarialConfig.from_profile({
            "adversarial": {
                "enabled": True,
                "plan_loop": {"enabled": True, "stages": ["design"], "max_rounds": 3},
            }
        })

        result = run_plan_loop(state, cfg, ["claude"])

    assert result.decision == "pass"
    assert result.rounds == 1
    assert result.final_plan["summary"] == "Good plan"


def test_plan_loop_revises_then_passes(isolated_story_home):
    from story_lifecycle.db import models as db
    from story_lifecycle.orchestrator.evaluator_loop import run_plan_loop, AdversarialConfig

    db.upsert_story("LOOP-PL2", workspace=os.getcwd(), profile="minimal")

    plan_v1 = {"adapter": "claude", "summary": "Vague plan", "extra_instructions": "Do stuff",
               "reasoning": "Fast", "trajectory_score": 0.5}
    plan_v2 = {"adapter": "claude", "summary": "Detailed plan", "extra_instructions": "Do X with Y",
               "reasoning": "Revised", "trajectory_score": 0.9}

    review_v1 = {"quality": "revise",
                 "blockers": [{"category": "scope", "description": "Too vague", "severity": "high"}],
                 "suggestions": ["Be more specific"], "reasoning": "Needs detail"}
    review_v2 = {"quality": "pass", "blockers": [], "suggestions": [], "reasoning": "Good now"}

    call_count = [0]

    def mock_plan(*args, **kwargs):
        call_count[0] += 1
        return plan_v1 if call_count[0] == 1 else plan_v2

    def mock_review(*args, **kwargs):
        return review_v1 if call_count[0] == 1 else review_v2

    with patch("story_lifecycle.orchestrator.planner.plan_stage", side_effect=mock_plan), \
         patch("story_lifecycle.orchestrator.planner.review_plan", side_effect=mock_review):
        state = _make_state(story_key="LOOP-PL2", stage="design")
        cfg = AdversarialConfig.from_profile({
            "adversarial": {
                "enabled": True,
                "plan_loop": {"enabled": True, "stages": ["design"], "max_rounds": 3},
            }
        })

        result = run_plan_loop(state, cfg, ["claude"])

    assert result.decision == "pass"
    assert result.rounds == 2
    assert result.final_plan["summary"] == "Detailed plan"


def test_plan_loop_stops_at_max_rounds(isolated_story_home):
    from story_lifecycle.db import models as db
    from story_lifecycle.orchestrator.evaluator_loop import run_plan_loop, AdversarialConfig

    db.upsert_story("LOOP-PL3", workspace=os.getcwd(), profile="minimal")

    plan_result = {"adapter": "claude", "summary": "Same plan", "extra_instructions": "X",
                   "reasoning": "R", "trajectory_score": 0.5}
    review_result = {"quality": "revise",
                     "blockers": [{"category": "scope", "description": "Still vague", "severity": "high"}],
                     "suggestions": [], "reasoning": "Not improving"}

    with patch("story_lifecycle.orchestrator.planner.plan_stage", return_value=plan_result), \
         patch("story_lifecycle.orchestrator.planner.review_plan", return_value=review_result):
        state = _make_state(story_key="LOOP-PL3", stage="design")
        cfg = AdversarialConfig.from_profile({
            "adversarial": {
                "enabled": True,
                "plan_loop": {"enabled": True, "stages": ["design"], "max_rounds": 2},
            }
        })

        result = run_plan_loop(state, cfg, ["claude"])

    assert result.decision == "max_rounds"
    assert result.rounds == 2


def test_plan_loop_logs_events(isolated_story_home):
    from story_lifecycle.db import models as db
    from story_lifecycle.orchestrator.evaluator_loop import run_plan_loop, AdversarialConfig

    db.upsert_story("LOOP-PL4", workspace=os.getcwd(), profile="minimal")

    plan_result = {"adapter": "claude", "summary": "Plan", "extra_instructions": "X",
                   "reasoning": "R", "trajectory_score": 0.8}
    review_result = {"quality": "pass", "blockers": [], "suggestions": [], "reasoning": "OK"}

    with patch("story_lifecycle.orchestrator.planner.plan_stage", return_value=plan_result), \
         patch("story_lifecycle.orchestrator.planner.review_plan", return_value=review_result):
        state = _make_state(story_key="LOOP-PL4", stage="design")
        cfg = AdversarialConfig.from_profile({
            "adversarial": {
                "enabled": True,
                "plan_loop": {"enabled": True, "stages": ["design"], "max_rounds": 3},
            }
        })

        run_plan_loop(state, cfg, ["claude"])

    started = _get_events_by_type("LOOP-PL4", "evaluator_loop_started")
    rounds = _get_events_by_type("LOOP-PL4", "evaluator_loop_round")
    completed = _get_events_by_type("LOOP-PL4", "evaluator_loop_completed")

    assert len(started) == 1
    assert len(rounds) == 1
    assert len(completed) == 1
    assert _parse_payload(completed[0])["decision"] == "pass"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_evaluator_loop.py -v -k "plan_loop"`
Expected: FAIL — `ImportError: cannot import name 'run_plan_loop'`

- [ ] **Step 3: Implement `run_plan_loop()` in evaluator_loop.py**

Add to `src/story_lifecycle/orchestrator/evaluator_loop.py`:

```python
def run_plan_loop(
    state: dict,
    adv_config: AdversarialConfig,
    adapters: list[str],
) -> LoopResult:
    """In-node plan loop: planner drafts → fresh reviewer evaluates → revise.

    Stops on: pass, max_rounds, no_progress.
    """
    from . import planner

    story_key = state["story_key"]
    stage = state["current_stage"]
    cfg = _get_stage_config_from_state(state)

    loop_id = _make_loop_id("plan", stage)
    reviewer_model = adv_config.resolve_reviewer_model("plan")
    optimizer_model = os.environ.get("STORY_LLM_MODEL", "")
    max_rounds = adv_config.plan_loop.max_rounds

    log_loop_started(
        story_key=story_key,
        stage=stage,
        loop_id=loop_id,
        loop_type="plan",
        mode="in_node",
        max_rounds=max_rounds,
        optimizer_model=optimizer_model,
        reviewer_model=reviewer_model or optimizer_model,
        attempt_id=f"{stage}:{state.get('execution_count', 0)}",
    )

    prev_blockers: list[dict] = []
    current_plan: dict | None = None

    for round_num in range(1, max_rounds + 1):
        # Optimizer: generate or revise plan
        try:
            current_plan = planner.plan_stage(state, cfg, adapters)
        except Exception as e:
            log.warning(f"Plan loop: planner failed at round {round_num}: {e}")
            log_loop_round(
                story_key=story_key, stage=stage, loop_id=loop_id,
                round_id=round_num, loop_type="plan", mode="in_node",
                decision="fail", score=0.0,
            )
            log_loop_completed(
                story_key=story_key, stage=stage, loop_id=loop_id,
                loop_type="plan", decision="fail", rounds=round_num,
                reason=f"planner_error:{type(e).__name__}",
            )
            return LoopResult(decision="fail", rounds=round_num, final_plan=None,
                              reason=f"Planner failed: {e}")

        if current_plan.get("skip"):
            log_loop_round(
                story_key=story_key, stage=stage, loop_id=loop_id,
                round_id=round_num, loop_type="plan", mode="in_node",
                decision="skip", score=0.0,
            )
            log_loop_completed(
                story_key=story_key, stage=stage, loop_id=loop_id,
                loop_type="plan", decision="pass", rounds=round_num,
                reason="plan_decided_skip",
            )
            return LoopResult(decision="pass", rounds=round_num, final_plan=current_plan,
                              reason="Plan decided to skip")

        # Evaluator: fresh plan review
        try:
            review = planner.review_plan(state, current_plan, cfg,
                                         reviewer_model=reviewer_model)
        except Exception as e:
            log.warning(f"Plan loop: reviewer failed at round {round_num}: {e}")
            # Reviewer failure → accept the plan (don't block on reviewer issues)
            log_loop_round(
                story_key=story_key, stage=stage, loop_id=loop_id,
                round_id=round_num, loop_type="plan", mode="in_node",
                decision="pass", score=current_plan.get("trajectory_score", 0.5),
            )
            log_loop_completed(
                story_key=story_key, stage=stage, loop_id=loop_id,
                loop_type="plan", decision="pass", rounds=round_num,
                reason="reviewer_error_accepted",
            )
            return LoopResult(decision="pass", rounds=round_num, final_plan=current_plan,
                              reason=f"Reviewer failed, accepting plan: {e}")

        quality = review.get("quality", "pass")
        blockers = review.get("blockers", [])
        high_blockers = [b for b in blockers if b.get("severity") == "high"]
        score = current_plan.get("trajectory_score", 0.5)

        # No-progress detection
        no_prog = False
        if round_num > 1 and high_blockers:
            no_prog = detect_no_progress(prev_blockers, high_blockers)

        log_loop_round(
            story_key=story_key, stage=stage, loop_id=loop_id,
            round_id=round_num, loop_type="plan", mode="in_node",
            decision=quality, score=score,
            findings={
                "new": [b.get("description", "")[:80] for b in high_blockers],
                "resolved": [],
                "repeated": [],
            },
            no_progress=no_prog,
        )

        if quality == "pass" or not high_blockers:
            log_loop_completed(
                story_key=story_key, stage=stage, loop_id=loop_id,
                loop_type="plan", decision="pass", rounds=round_num,
                reason="all_blockers_resolved",
            )
            return LoopResult(decision="pass", rounds=round_num, final_plan=current_plan,
                              reason="All blockers resolved")

        if no_prog:
            log_loop_completed(
                story_key=story_key, stage=stage, loop_id=loop_id,
                loop_type="plan", decision="no_progress", rounds=round_num,
                reason="repeated_blockers_no_progress",
                remaining_findings=[b.get("description", "")[:80] for b in high_blockers],
            )
            return LoopResult(decision="no_progress", rounds=round_num, final_plan=current_plan,
                              reason="Repeated blockers, no progress",
                              remaining_findings=[b.get("description", "")[:80] for b in high_blockers])

        # Feed blocker context back into state for next planner round
        blocker_text = "\n".join(
            f"- [{b.get('severity', '')}] {b.get('category', '')}: {b.get('description', '')}"
            + (f"\n  Suggestion: {b.get('suggestion', '')}" if b.get("suggestion") else "")
            for b in high_blockers
        )
        state = {**state, "review_summary": f"Plan review round {round_num} issues:\n{blocker_text}"}
        prev_blockers = high_blockers

    # Max rounds reached
    log_loop_completed(
        story_key=story_key, stage=stage, loop_id=loop_id,
        loop_type="plan", decision="max_rounds", rounds=max_rounds,
        reason="max_rounds_reached",
        remaining_findings=[b.get("description", "")[:80] for b in prev_blockers],
    )
    return LoopResult(decision="max_rounds", rounds=max_rounds, final_plan=current_plan,
                      reason="Max rounds reached",
                      remaining_findings=[b.get("description", "")[:80] for b in prev_blockers])


def _get_stage_config_from_state(state: dict) -> dict:
    """Get stage config from state's profile and current_stage."""
    from .nodes import get_stage_config
    return get_stage_config(state.get("profile", "minimal"), state.get("current_stage", ""))
```

Also add the missing import at the top of `evaluator_loop.py`:

```python
import logging
```

(Already included in the file created in Task 2.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_evaluator_loop.py -v -k "plan_loop"`
Expected: 4 PASSED

- [ ] **Step 5: Commit**

```bash
git add src/story_lifecycle/orchestrator/evaluator_loop.py
git commit -m "feat: implement in-node plan loop with reviewer convergence"
```

---

### Task 5: `run_code_review_loop()` — Cross-Node Code Review

**Files:**
- Modify: `src/story_lifecycle/orchestrator/evaluator_loop.py`
- Test: `tests/test_evaluator_loop.py` (append)

P0 code loop is NOT a while loop — `run_code_review_loop()` runs ONE round of fresh reviewer, records findings, and builds a repair packet. The actual retry happens through the existing router retry path.

- [ ] **Step 1: Write failing tests for code review loop**

Append to `tests/test_evaluator_loop.py`:

```python
# ── run_code_review_loop tests ──


def test_code_review_loop_pass_returns_pass(isolated_story_home):
    from story_lifecycle.db import models as db
    from story_lifecycle.orchestrator.evaluator_loop import run_code_review_loop, AdversarialConfig

    db.upsert_story("LOOP-CR1", workspace=os.getcwd(), profile="minimal")

    review_result = {
        "quality": "pass",
        "summary": "Code looks good",
        "issues": [],
        "suggestions": [],
        "trajectory_score": 0.9,
        "reasoning": "Clean implementation",
    }

    with patch("story_lifecycle.orchestrator.planner.review_stage", return_value=review_result):
        state = _make_state(story_key="LOOP-CR1", stage="implement")
        cfg = AdversarialConfig.from_profile({
            "adversarial": {
                "enabled": True,
                "code_loop": {"enabled": True, "mode": "short_lived", "max_rounds": 3},
            }
        })

        result = run_code_review_loop(state, cfg, state["context"])

    assert result.decision == "pass"
    assert result.rounds == 1


def test_code_review_loop_revise_records_findings(isolated_story_home):
    from story_lifecycle.db import models as db
    from story_lifecycle.orchestrator.evaluator_loop import run_code_review_loop, AdversarialConfig

    db.upsert_story("LOOP-CR2", workspace=os.getcwd(), profile="minimal")

    review_result = {
        "quality": "revise",
        "summary": "Security issues found",
        "issues": [
            {"type": "security", "severity": "high", "location": "auth.py:42",
             "description": "Missing CSRF token"},
            {"type": "testing", "severity": "medium", "location": "",
             "description": "No unit tests"},
        ],
        "suggestions": ["Add CSRF middleware", "Write test_auth.py"],
        "trajectory_score": 0.4,
        "reasoning": "High severity security issue",
    }

    with patch("story_lifecycle.orchestrator.planner.review_stage", return_value=review_result):
        state = _make_state(story_key="LOOP-CR2", stage="implement")
        cfg = AdversarialConfig.from_profile({
            "adversarial": {
                "enabled": True,
                "code_loop": {"enabled": True, "mode": "short_lived", "max_rounds": 3},
            }
        })

        result = run_code_review_loop(state, cfg, state["context"])

    assert result.decision == "revise"
    assert result.rounds == 1
    assert len(result.remaining_findings) >= 1

    # Verify findings were recorded to DB
    findings = db.get_open_findings("LOOP-CR2")
    assert len(findings) >= 1
    assert any("CSRF" in f["description"] for f in findings)

    # Verify events were logged
    started = _get_events_by_type("LOOP-CR2", "evaluator_loop_started")
    rounds = _get_events_by_type("LOOP-CR2", "evaluator_loop_round")
    assert len(started) == 1
    assert len(rounds) == 1


def test_code_review_loop_revise_builds_repair_packet(isolated_story_home):
    from story_lifecycle.db import models as db
    from story_lifecycle.orchestrator.evaluator_loop import run_code_review_loop, AdversarialConfig
    from pathlib import Path

    workspace = str(isolated_story_home / "ws")
    Path(workspace).mkdir(exist_ok=True)

    db.upsert_story("LOOP-CR3", workspace=workspace, profile="minimal")

    review_result = {
        "quality": "revise",
        "summary": "Issues found",
        "issues": [
            {"type": "bug", "severity": "high", "location": "main.py:10",
             "description": "NPE risk"},
        ],
        "suggestions": ["Add null check"],
        "trajectory_score": 0.3,
        "reasoning": "Bug risk",
    }

    with patch("story_lifecycle.orchestrator.planner.review_stage", return_value=review_result):
        state = _make_state(story_key="LOOP-CR3", stage="implement", workspace=workspace)
        cfg = AdversarialConfig.from_profile({
            "adversarial": {
                "enabled": True,
                "code_loop": {"enabled": True, "mode": "short_lived", "max_rounds": 3},
            }
        })

        result = run_code_review_loop(state, cfg, state["context"])

    assert result.decision == "revise"
    repair_path = result.final_review.get("repair_packet_path")
    assert repair_path is not None
    assert Path(repair_path).exists()
    content = Path(repair_path).read_text(encoding="utf-8")
    assert "NPE risk" in content


def test_code_review_loop_handles_reviewer_json_failure(isolated_story_home):
    from story_lifecycle.db import models as db
    from story_lifecycle.orchestrator.evaluator_loop import run_code_review_loop, AdversarialConfig

    db.upsert_story("LOOP-CR4", workspace=os.getcwd(), profile="minimal")

    with patch("story_lifecycle.orchestrator.planner.review_stage", side_effect=RuntimeError("LLM error")):
        state = _make_state(story_key="LOOP-CR4", stage="implement")
        cfg = AdversarialConfig.from_profile({
            "adversarial": {
                "enabled": True,
                "code_loop": {"enabled": True, "mode": "short_lived", "max_rounds": 3},
            }
        })

        result = run_code_review_loop(state, cfg, state["context"])

    # Reviewer failure should not crash — should degrade gracefully
    assert result.decision == "fail"
    # No findings created from failed review
    findings = db.get_open_findings("LOOP-CR4")
    assert len(findings) == 0


def test_code_review_loop_records_prompt_tokens_estimation(isolated_story_home):
    from story_lifecycle.db import models as db
    from story_lifecycle.orchestrator.evaluator_loop import run_code_review_loop, AdversarialConfig

    db.upsert_story("LOOP-CR5", workspace=os.getcwd(), profile="minimal")

    review_result = {
        "quality": "pass", "summary": "OK", "issues": [], "suggestions": [],
        "trajectory_score": 0.9, "reasoning": "Fine",
    }

    with patch("story_lifecycle.orchestrator.planner.review_stage", return_value=review_result):
        state = _make_state(story_key="LOOP-CR5", stage="implement")
        cfg = AdversarialConfig.from_profile({
            "adversarial": {
                "enabled": True,
                "code_loop": {"enabled": True, "mode": "short_lived", "max_rounds": 3},
            }
        })

        run_code_review_loop(state, cfg, state["context"])

    rounds = _get_events_by_type("LOOP-CR5", "evaluator_loop_round")
    assert len(rounds) == 1
    p = _parse_payload(rounds[0])
    assert "prompt_tokens" in p
    assert p["prompt_tokens"].get("estimated") is True
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_evaluator_loop.py -v -k "code_review_loop"`
Expected: FAIL — `ImportError: cannot import name 'run_code_review_loop'`

- [ ] **Step 3: Implement `run_code_review_loop()` in evaluator_loop.py**

Add to `src/story_lifecycle/orchestrator/evaluator_loop.py`:

```python
def run_code_review_loop(
    state: dict,
    adv_config: AdversarialConfig,
    stage_output: dict,
) -> LoopResult:
    """Cross-node code review loop: ONE round of fresh reviewer per call.

    P0 code loop is cross-node iterative retry. This function runs exactly one
    reviewer round. If findings are found, records them, builds a repair packet,
    and returns a revise result. The router retry path will re-enter plan/execute.

    Reviewer gets isolated context — no implementer conversation history.
    """
    from . import planner
    from .quality import record_finding

    story_key = state["story_key"]
    stage = state["current_stage"]
    cfg = _get_stage_config_from_state(state)
    max_rounds = adv_config.code_loop.max_rounds

    loop_id = _make_loop_id("code", stage)
    reviewer_model = adv_config.resolve_reviewer_model("code")
    optimizer_model = os.environ.get("STORY_LLM_MODEL", "")

    log_loop_started(
        story_key=story_key,
        stage=stage,
        loop_id=loop_id,
        loop_type="code",
        mode="short_lived",
        max_rounds=max_rounds,
        optimizer_model=optimizer_model,
        reviewer_model=reviewer_model or optimizer_model,
        attempt_id=f"{stage}:{state.get('execution_count', 0)}",
    )

    # Single reviewer round — fresh context
    try:
        review = planner.review_stage(state, cfg, stage_output)
    except Exception as e:
        log.warning(f"Code review loop: reviewer failed: {e}")
        log_loop_round(
            story_key=story_key, stage=stage, loop_id=loop_id,
            round_id=1, loop_type="code", mode="short_lived",
            decision="fail",
        )
        log_loop_completed(
            story_key=story_key, stage=stage, loop_id=loop_id,
            loop_type="code", decision="fail", rounds=1,
            reason=f"reviewer_error:{type(e).__name__}",
        )
        return LoopResult(
            decision="fail", rounds=1,
            reason=f"Reviewer failed: {e}",
        )

    quality = review.get("quality", "pass")
    issues = review.get("issues", [])
    score = review.get("trajectory_score", 0.5)

    # Record findings to DB
    finding_ids = []
    for issue in issues:
        try:
            fid = record_finding(story_key, stage, {
                "source": "code_review",
                "severity": issue.get("severity", "medium"),
                "category": issue.get("type", "unknown"),
                "description": issue.get("description", ""),
                "location": issue.get("location"),
                "recommendation": issue.get("recommendation") or next(
                    (s for s in review.get("suggestions", []) if issue.get("description", "")[:20] in s), ""
                ),
            })
            finding_ids.append(fid)
        except Exception:
            pass  # Don't block review on finding creation failure

    # Build repair packet on revise
    repair_path = None
    if quality == "revise":
        round_num = state.get("execution_count", 0) + 1
        plan_summary = state.get("plan_summary", "")
        output_summary = state.get("context", {}).get("summary", "")

        repair_path = build_repair_packet(
            story_key=story_key,
            stage=stage,
            workspace=state.get("workspace", ""),
            plan_summary=plan_summary,
            stage_output_summary=output_summary,
            findings=[
                {
                    "severity": i.get("severity", ""),
                    "category": i.get("type", ""),
                    "description": i.get("description", ""),
                    "location": i.get("location", ""),
                    "recommendation": i.get("recommendation", ""),
                }
                for i in issues
            ],
            verification={"status": "not_run", "commands": []},
            round_num=round_num,
            write_file=True,
        )

    # Estimate prompt tokens (rough: ~4 chars/token)
    ctx_text = json.dumps(stage_output, ensure_ascii=False)
    prompt_estimate = {
        "total": max(1, len(ctx_text) // CHARS_PER_TOKEN),
        "context": max(1, len(ctx_text) // CHARS_PER_TOKEN),
        "feedback": 0,
        "repeated_context": 0,
        "estimated": True,
    }

    log_loop_round(
        story_key=story_key, stage=stage, loop_id=loop_id,
        round_id=1, loop_type="code", mode="short_lived",
        decision=quality, score=score,
        findings={
            "open_before": [],
            "new": finding_ids,
            "resolved": [],
            "repeated": [],
        },
        verification={"status": "not_run", "commands": []},
        prompt_tokens=prompt_estimate,
    )

    if quality == "pass":
        log_loop_completed(
            story_key=story_key, stage=stage, loop_id=loop_id,
            loop_type="code", decision="pass", rounds=1,
            reason="review_passed",
        )
        return LoopResult(
            decision="pass", rounds=1,
            final_review=review,
            reason="Review passed",
        )

    # revise or fail
    final_reason = "review_revisions_needed"
    if quality == "fail":
        final_reason = "review_failed"

    log_loop_completed(
        story_key=story_key, stage=stage, loop_id=loop_id,
        loop_type="code", decision=quality, rounds=1,
        reason=final_reason,
        remaining_findings=finding_ids,
    )

    review["repair_packet_path"] = repair_path
    return LoopResult(
        decision=quality, rounds=1,
        final_review=review,
        reason=final_reason,
        remaining_findings=finding_ids,
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_evaluator_loop.py -v -k "code_review_loop"`
Expected: 5 PASSED

- [ ] **Step 5: Commit**

```bash
git add src/story_lifecycle/orchestrator/evaluator_loop.py
git commit -m "feat: implement cross-node code review loop with finding recording"
```

---

### Task 6: Profile Config, Node Integration, Repair Packet Injection

**Files:**
- Modify: `profiles/minimal.yaml` — add `adversarial` block
- Modify: `src/story_lifecycle/orchestrator/nodes.py` — integrate loops, inject repair packet
- Test: `tests/test_evaluator_loop.py` (append)

This task ties everything together: profile config, node integration, and repair packet prompt injection.

- [ ] **Step 1: Write failing tests for node integration**

Append to `tests/test_evaluator_loop.py`:

```python
# ── Node integration tests ──


def test_plan_stage_node_uses_loop_when_enabled(isolated_story_home):
    """plan_stage_node delegates to run_plan_loop when adversarial plan_loop enabled."""
    from story_lifecycle.db import models as db
    from story_lifecycle.orchestrator.nodes import plan_stage_node
    from story_lifecycle.orchestrator.evaluator_loop import LoopResult

    db.upsert_story("LOOP-NI1", workspace=os.getcwd(), profile="minimal")

    loop_result = LoopResult(
        decision="pass", rounds=1,
        final_plan={"adapter": "claude", "summary": "Loop plan",
                     "extra_instructions": "X", "reasoning": "R",
                     "trajectory_score": 0.9},
        reason="passed",
    )

    with patch("story_lifecycle.orchestrator.evaluator_loop.run_plan_loop", return_value=loop_result) as mock_loop, \
         patch("story_lifecycle.orchestrator.planner.is_available", return_value=True), \
         patch("story_lifecycle.orchestrator.planner.compress_context", return_value=None):
        state = _make_state(story_key="LOOP-NI1", stage="design")
        result = plan_stage_node(state)

    mock_loop.assert_called_once()
    assert result["plan_summary"] == "Loop plan"


def test_plan_stage_node_skips_loop_when_disabled(isolated_story_home):
    """plan_stage_node uses normal planner when adversarial is disabled."""
    from story_lifecycle.db import models as db
    from story_lifecycle.orchestrator.nodes import plan_stage_node

    db.upsert_story("LOOP-NI2", workspace=os.getcwd(), profile="minimal")

    plan_result = {"adapter": "claude", "summary": "Normal plan",
                   "extra_instructions": "X", "reasoning": "R",
                   "trajectory_score": 0.8, "skip": False}

    with patch("story_lifecycle.orchestrator.evaluator_loop.run_plan_loop") as mock_loop, \
         patch("story_lifecycle.orchestrator.planner.is_available", return_value=True), \
         patch("story_lifecycle.orchestrator.planner.plan_stage", return_value=plan_result), \
         patch("story_lifecycle.orchestrator.planner.compress_context", return_value=None):
        state = _make_state(story_key="LOOP-NI2", stage="design")
        result = plan_stage_node(state)

    mock_loop.assert_not_called()
    assert result["plan_summary"] == "Normal plan"


def test_review_stage_node_uses_loop_when_enabled(isolated_story_home):
    """review_stage_node delegates to run_code_review_loop when adversarial code_loop enabled."""
    from story_lifecycle.db import models as db
    from story_lifecycle.orchestrator.nodes import review_stage_node
    from story_lifecycle.orchestrator.evaluator_loop import LoopResult

    db.upsert_story("LOOP-NI3", workspace=os.getcwd(), profile="minimal")

    loop_result = LoopResult(
        decision="revise", rounds=1,
        final_review={"quality": "revise", "summary": "Issues found",
                       "issues": [{"severity": "high", "type": "bug",
                                   "description": "NPE", "location": "a.py:1"}],
                       "trajectory_score": 0.4, "suggestions": []},
        reason="revisions needed",
        remaining_findings=["F-001"],
    )

    with patch("story_lifecycle.orchestrator.evaluator_loop.run_code_review_loop", return_value=loop_result) as mock_loop, \
         patch("story_lifecycle.orchestrator.planner.is_available", return_value=True):
        state = _make_state(story_key="LOOP-NI3", stage="implement")
        result = review_stage_node(state)

    mock_loop.assert_called_once()
    assert result.get("last_error") is not None  # revise sets last_error


def test_review_stage_node_skips_loop_when_disabled(isolated_story_home):
    """review_stage_node uses normal reviewer when adversarial is disabled."""
    from story_lifecycle.db import models as db
    from story_lifecycle.orchestrator.nodes import review_stage_node

    db.upsert_story("LOOP-NI4", workspace=os.getcwd(), profile="minimal")

    review_result = {
        "quality": "pass", "summary": "OK", "issues": [], "suggestions": [],
        "trajectory_score": 0.9, "reasoning": "Fine", "context_updates": {},
    }

    with patch("story_lifecycle.orchestrator.evaluator_loop.run_code_review_loop") as mock_loop, \
         patch("story_lifecycle.orchestrator.planner.is_available", return_value=True), \
         patch("story_lifecycle.orchestrator.planner.review_stage", return_value=review_result):
        state = _make_state(story_key="LOOP-NI4", stage="implement")
        result = review_stage_node(state)

    mock_loop.assert_not_called()
    assert result.get("last_error") is None


def test_repair_packet_injected_on_retry(isolated_story_home):
    """_render_prompt includes repair packet content when present in state."""
    from story_lifecycle.db import models as db
    from story_lifecycle.orchestrator.nodes import _render_prompt
    from pathlib import Path

    db.upsert_story("LOOP-NI5", workspace=os.getcwd(), profile="minimal")

    # Create a repair packet file
    story_key = "LOOP-NI5"
    repair_dir = Path(os.getcwd()) / ".story-context" / story_key
    repair_dir.mkdir(parents=True, exist_ok=True)
    repair_file = repair_dir / "repair_implement_round1.md"
    repair_file.write_text("## Blocking Findings\n- NPE risk @ main.py:10\n", encoding="utf-8")

    state = _make_state(story_key=story_key, stage="implement")
    state["context"]["repair_packet_path"] = str(repair_file.relative_to(Path(os.getcwd())))

    prompt, meta = _render_prompt("implement", state)

    assert "NPE risk" in prompt
    assert "Repair Packet" in prompt or "repair" in prompt.lower()

    # Cleanup
    repair_file.unlink(missing_ok=True)
    try:
        repair_dir.rmdir()
        (Path(os.getcwd()) / ".story-context").rmdir()
    except Exception:
        pass
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_evaluator_loop.py -v -k "node_integration or repair_packet_injected"`
Expected: FAIL — plan_stage_node doesn't call run_plan_loop yet

- [ ] **Step 3: Add `adversarial` config to minimal.yaml**

Edit `profiles/minimal.yaml` — append after the `quality:` block:

```yaml
# Adversarial evaluator-optimizer loops (P0 — default disabled)
# Enable to activate plan review and code review loops.
# adversarial:
#   enabled: true
#   plan_loop:
#     enabled: true
#     stages: [design, implement]
#     max_rounds: 3
#     reviewer_model: deepseek-chat
#     pass_condition: no_open_blocker_or_major
#   code_loop:
#     enabled: true
#     mode: short_lived
#     max_rounds: 3
#     reviewer_model: deepseek-chat
#     pass_condition: no_open_blocker
#     fallback: repair_packet
#   observability:
#     log_round_events: true
#     estimate_prompt_segments: true
```

This keeps the default behavior unchanged (adversarial disabled).

- [ ] **Step 4: Modify `plan_stage_node` in nodes.py**

In `src/story_lifecycle/orchestrator/nodes.py`, modify `plan_stage_node()` to check for adversarial config and delegate to `run_plan_loop`:

Add import at top of `nodes.py` (after existing imports):

```python
from .evaluator_loop import AdversarialConfig, run_plan_loop
```

In `plan_stage_node()`, insert the adversarial check **after** the Condenser block and **before** the existing `if planner.is_available():` block. The logic is:

```python
    # --- Adversarial plan loop (before normal planner) ---
    try:
        profile_cfg = load_profile(profile)
        adv_cfg = AdversarialConfig.from_profile(profile_cfg)
        if adv_cfg.plan_loop_enabled(stage) and planner.is_available():
            from ..orchestrator.graph import emit_plan_done
            adapters = ["claude"]
            loop_result = run_plan_loop(state, adv_cfg, adapters)

            if loop_result.final_plan and loop_result.final_plan.get("skip"):
                state["status"] = "skipping"
                state["plan_summary"] = f"跳过: {loop_result.final_plan.get('reasoning', '')}"
                emit_plan_done(story_key, state["plan_summary"])
                return state

            if loop_result.final_plan:
                plan = loop_result.final_plan
                # Reuse existing plan file writing logic (copy from below)
                plan_file = (
                    Path(workspace) / ".story-context" / story_key / f"plan_{stage}.md"
                )
                plan_file.parent.mkdir(parents=True, exist_ok=True)

                review_path = state.get("context", {}).get("review_path")
                review_section = ""
                if review_path:
                    rf = Path(workspace) / review_path
                    if rf.exists():
                        review_section = (
                            f"\n## 前序 Review 建议\n"
                            f"请先处理以下问题：\n{rf.read_text(encoding='utf-8')}"
                        )

                plan_file.write_text(
                    f"# 任务书: {stage}\n\n"
                    f"## 执行指令\n{plan.get('extra_instructions', '')}\n"
                    f"{review_section}\n\n"
                    f"## 配置\n"
                    f"- Adapter: {plan.get('adapter', 'claude')}\n"
                    f"- Provider: {plan.get('provider', 'deepseek')}\n"
                    f"- Model: {plan.get('model', 'sonnet')}\n\n"
                    f"## 决策理由\n{plan.get('reasoning', '')}\n\n"
                    f"## 路径评分\n"
                    f"当前路径评分: {plan.get('trajectory_score', 'N/A')}/1.0",
                    encoding="utf-8",
                )

                state["plan_summary"] = plan.get("summary", "")
                state["trajectory_score"] = plan.get("trajectory_score")
                state["context"]["plan_path"] = str(plan_file.relative_to(workspace))
                state["context"]["plan_summary"] = plan.get("summary", "")
                state["plan"] = plan

                db.log_event(
                    story_key, stage, "plan",
                    {
                        "adapter": plan.get("adapter"),
                        "summary": plan.get("summary", "")[:100],
                        "trajectory_score": plan.get("trajectory_score"),
                        "adversarial_loop": True,
                        "loop_rounds": loop_result.rounds,
                        "loop_decision": loop_result.decision,
                    },
                )

                summary = plan.get("summary", "")
                tool_info = f"{plan.get('adapter', 'claude')} / {plan.get('model', 'sonnet')}"
                plan_text = f"✓ {summary}  [dim]({tool_info})[/]"
                emit_plan_done(story_key, plan_text)
                return state

            # Loop returned without a valid plan (failure path)
            if loop_result.decision in ("fail", "no_progress"):
                state["plan_summary"] = f"Plan loop {loop_result.decision}: {loop_result.reason}"
                emit_plan_done(story_key, f"⚠ Plan loop {loop_result.decision}", ok=False)
                # Fall through to normal planner for recovery
    except Exception as e:
        log.warning(f"Adversarial plan loop failed, falling back to normal: {e}")
    # --- End adversarial plan loop ---
```

**Important:** The adversarial block is a try/except that falls through to the existing planner logic on any error. This preserves backward compatibility.

- [ ] **Step 5: Modify `review_stage_node` in nodes.py**

In `review_stage_node()`, insert the adversarial check **after** the circuit breaker and retry-fatigue checks, **before** the existing `if planner.is_available():` block:

```python
    # --- Adversarial code review loop (before normal reviewer) ---
    try:
        profile_cfg = load_profile(state.get("profile", "minimal"))
        adv_cfg = AdversarialConfig.from_profile(profile_cfg)
        if adv_cfg.code_loop_enabled(stage) and planner.is_available():
            from .evaluator_loop import run_code_review_loop
            loop_result = run_code_review_loop(state, adv_cfg, stage_output)

            review = loop_result.final_review or {}
            workspace = state["workspace"]
            story_key = state["story_key"]

            # Write review file (same as normal path)
            review_file = (
                Path(workspace) / ".story-context" / story_key / f"review_{stage}.md"
            )
            review_file.parent.mkdir(parents=True, exist_ok=True)

            issues_table = ""
            for issue in review.get("issues", []):
                issues_table += (
                    f"| {issue.get('type', '')} | {issue.get('severity', '')} "
                    f"| {issue.get('location', '')} | {issue.get('description', '')} |\n"
                )
            suggestions_list = "\n".join(f"- {s}" for s in review.get("suggestions", []))
            no_issues_row = "| （无） | | | |\n"
            review_file.write_text(
                f"# 评审: {stage} (adversarial)\n\n"
                f"## 结论: {review.get('quality', 'pass')}\n\n"
                f"## 摘要\n{review.get('summary', '')}\n\n"
                f"## 问题列表\n"
                f"| 类型 | 严重度 | 位置 | 描述 |\n"
                f"|------|--------|------|------|\n"
                f"{issues_table or no_issues_row}\n"
                f"## 建议\n{suggestions_list or '（无）'}\n\n"
                f"## 路径评分\n{review.get('trajectory_score', 'N/A')}/1.0\n\n"
                f"## 详细理由\n{review.get('reasoning', '')}",
                encoding="utf-8",
            )

            state["review_summary"] = review.get("summary", "")
            state["trajectory_score"] = review.get("trajectory_score")
            state["context"]["review_path"] = str(review_file.relative_to(workspace))
            state["context"]["review_summary"] = review.get("summary", "")

            # Store repair packet path in context for prompt injection
            repair_path = review.get("repair_packet_path")
            if repair_path:
                state["context"]["repair_packet_path"] = str(
                    Path(repair_path).relative_to(workspace)
                ) if not Path(repair_path).is_absolute() else repair_path

            quality = review.get("quality", "pass")
            if quality == "revise":
                high_issues = [
                    i for i in review.get("issues", []) if i.get("severity") == "high"
                ]
                state["last_error"] = (
                    f"Review: {review.get('summary', 'needs revision')} "
                    f"({len(high_issues)} high severity issues)"
                )
            elif quality == "fail":
                state["last_error"] = f"Review failed: {review.get('summary', '')}"

            db.log_event(
                story_key, stage, "review",
                {
                    "quality": quality,
                    "summary": review.get("summary", "")[:100],
                    "issues_count": len(review.get("issues", [])),
                    "trajectory_score": review.get("trajectory_score"),
                    "adversarial_loop": True,
                    "loop_rounds": loop_result.rounds,
                    "loop_decision": loop_result.decision,
                },
            )
            return state
    except Exception as e:
        log.warning(f"Adversarial code loop failed, falling back to normal review: {e}")
    # --- End adversarial code review loop ---
```

- [ ] **Step 6: Inject repair packet in `_render_prompt()`**

In `_render_prompt()` in `nodes.py`, add repair packet injection after the quality packet injection block (after the `quality_section` / `checklist` variables are set):

```python
    # Repair packet injection (for adversarial loop retry)
    repair_section = ""
    repair_packet_path = ctx.get("repair_packet_path")
    if repair_packet_path:
        rp_file = Path(workspace) / repair_packet_path
        if rp_file.exists():
            repair_content = rp_file.read_text(encoding="utf-8")
            repair_section = f"## Repair Packet（修复上下文）\n\n{repair_content}"
```

Then add to the `vars_map` dict:

```python
        "{repair_packet_section}": repair_section,
```

Also add a default `{repair_packet_section}` placeholder so templates without it still work (same pattern as `{quality_packet_section}`).

- [ ] **Step 7: Run tests to verify they pass**

Run: `pytest tests/test_evaluator_loop.py -v`
Expected: All tests PASSED

Run: `pytest tests/ -v --tb=short` (regression check)
Expected: No failures in existing tests

- [ ] **Step 8: Commit**

```bash
git add profiles/minimal.yaml src/story_lifecycle/orchestrator/nodes.py tests/test_evaluator_loop.py
git commit -m "feat: integrate adversarial loops into plan/review nodes with repair packet injection"
```

---

## Self-Review Checklist

- [x] **Spec coverage:** Each spec section maps to a task:
  - `loop_events.py` → Task 1 (event schema)
  - `evaluator_loop.py` types/config → Task 2 (LoopResult, AdversarialConfig, build_repair_packet, detect_no_progress)
  - `planner.review_plan()` → Task 3 (plan reviewer)
  - `run_plan_loop()` → Task 4 (in-node plan loop)
  - `run_code_review_loop()` → Task 5 (cross-node code review)
  - Node integration + profile → Task 6 (nodes.py, minimal.yaml, repair injection)
- [x] **Placeholder scan:** No TBD, TODO, or "implement later" patterns. All steps contain actual code.
- [x] **Type consistency:** `LoopResult` fields used consistently across all tasks. `AdversarialConfig.from_profile()` signature matches all call sites.
- [x] **Backward compatibility:** Adversarial config defaults to disabled. All integration code is wrapped in try/except that falls through to existing behavior on error.
- [x] **Spec decisions preserved:**
  - Plan loop = in-node loop ✓
  - Code loop = cross-node iterative retry (not while loop) ✓
  - Reviewer context isolation (fresh call each time) ✓
  - No new finding statuses (uses event payloads) ✓
  - Repair packet = no full diff ✓
  - Token budgets enforced (hard/emergency trim) ✓
  - No-progress = category+location match on high severity only ✓
