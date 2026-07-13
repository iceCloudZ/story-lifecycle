# Quality Flywheel P0 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans.

**Goal:** 实现 P0 — 本地闭环。finding 表、quality.py、event 记录、Quality Packet 生成、prompt 注入。

**Architecture:** 新增 `orchestrator/quality.py` 模块。DB 新增 `finding` 表。nodes.py 注入 Quality Packet 和 Checklist。service.py 记录 story_intake 事件。

**Tech Stack:** Python 3.11+, SQLite, Textual TUI

---

## File Structure

| File | Action | Responsibility |
|------|--------|----------------|
| `src/story_lifecycle/db/models.py` | Modify | 新增 finding 表 + 查询函数 |
| `src/story_lifecycle/orchestrator/quality.py` | Create | 核心质量飞轮逻辑 |
| `src/story_lifecycle/orchestrator/nodes.py` | Modify | prompt 注入 Quality Packet + Checklist |
| `src/story_lifecycle/orchestrator/service.py` | Modify | create_story_from_source 记录 story_intake |
| `tests/test_quality_flywheel.py` | Create | P0 测试 |

---

### Task 1: DB — finding 表 + 查询函数

**Files:**
- Modify: `src/story_lifecycle/db/models.py`
- Create: `tests/test_quality_flywheel.py`

- [ ] **Step 1: Write failing test**

```python
def test_finding_lifecycle(tmp_path):
    """Finding should support full lifecycle: open -> accepted -> fixed -> verified -> learned."""
    from story_lifecycle.db.models import Database, get_db, set_db_path
    original = set_db_path(str(tmp_path / "test.db"))
    db = get_db()
    try:
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
    finally:
        set_db_path(original)
```

- [ ] **Step 2: Run test to verify failure**

- [ ] **Step 3: Implement**

In `src/story_lifecycle/db/models.py`:

a) Add `finding` table creation in `init_db`:

```python
self._conn.execute("""
    CREATE TABLE IF NOT EXISTS finding (
        id TEXT PRIMARY KEY,
        story_key TEXT NOT NULL,
        stage TEXT,
        source TEXT NOT NULL,
        severity TEXT NOT NULL,
        category TEXT NOT NULL,
        location TEXT,
        description TEXT NOT NULL,
        recommendation TEXT,
        root_cause TEXT,
        status TEXT NOT NULL DEFAULT 'open',
        verification_event_id INTEGER,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
""")
self._conn.execute("CREATE INDEX IF NOT EXISTS idx_finding_story_status ON finding(story_key, status)")
self._conn.execute("CREATE INDEX IF NOT EXISTS idx_finding_severity ON finding(severity, status)")
```

b) Add methods:

```python
def create_finding(self, story_key, stage, source, severity, category, description,
                   location=None, recommendation=None, root_cause=None) -> str:
    from datetime import datetime
    fid = f"finding-{datetime.now().strftime('%Y%m%d%H%M%S')}-{id(self) % 10000:04d}"
    now = datetime.now().isoformat()
    self._conn.execute(
        "INSERT INTO finding (id, story_key, stage, source, severity, category, location, description, recommendation, root_cause, status, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (fid, story_key, stage, source, severity, category, location, description, recommendation, root_cause, "open", now, now),
    )
    self._conn.commit()
    return fid

def get_finding(self, finding_id: str) -> dict | None:
    rows = self._conn.execute("SELECT * FROM finding WHERE id = ?", (finding_id,)).fetchall()
    return dict(rows[0]) if rows else None

def update_finding(self, finding_id: str, **kwargs) -> None:
    from datetime import datetime
    kwargs["updated_at"] = datetime.now().isoformat()
    sets = ", ".join(f"{k} = ?" for k in kwargs)
    vals = list(kwargs.values()) + [finding_id]
    self._conn.execute(f"UPDATE finding SET {sets} WHERE id = ?", vals)
    self._conn.commit()

def get_open_findings(self, story_key: str, min_severity: str = "medium") -> list[dict]:
    severity_order = {"high": 3, "medium": 2, "low": 1}
    min_level = severity_order.get(min_severity, 2)
    rows = self._conn.execute(
        "SELECT * FROM finding WHERE story_key = ? AND status = 'open'",
        (story_key,),
    ).fetchall()
    return [dict(r) for r in rows if severity_order.get(r["severity"], 0) >= min_level]

def get_learned_patterns_for(self, applies_to: list[str] | None = None, limit: int = 5) -> list[dict]:
    """Get active learned patterns from event_log."""
    rows = self._conn.execute(
        "SELECT * FROM stage_log WHERE action = 'learned_pattern' ORDER BY created_at DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return [dict(r) for r in rows]
```

- [ ] **Step 4: Run tests**

- [ ] **Step 5: Commit**

```bash
git add src/story_lifecycle/db/models.py tests/test_quality_flywheel.py
git commit -m "feat: add finding table and lifecycle queries for quality flywheel

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

### Task 2: quality.py — 核心飞轮逻辑

**Files:**
- Create: `src/story_lifecycle/orchestrator/quality.py`

- [ ] **Step 1: Create quality.py**

```python
# src/story_lifecycle/orchestrator/quality.py
from __future__ import annotations

import json
from datetime import datetime

from ..db import models as db


def record_finding(story_key: str, stage: str, finding: dict) -> str:
    """Create a finding + write code_review_finding event."""
    fid = db.create_finding(
        story_key=story_key,
        stage=stage,
        source=finding.get("source", "code_review"),
        severity=finding["severity"],
        category=finding["category"],
        description=finding["description"],
        location=finding.get("location"),
        recommendation=finding.get("recommendation"),
        root_cause=finding.get("root_cause"),
    )
    db.log_event(story_key, stage, "code_review_finding", {"finding_id": fid, **finding})
    return fid


def update_finding_status(
    story_key: str,
    finding_id: str,
    status: str,
    reason: str = "",
    evidence: dict | None = None,
) -> None:
    """Update finding status + write audit event."""
    old = db.get_finding(finding_id)
    old_status = old["status"] if old else "unknown"

    kwargs = {"status": status}
    if evidence and evidence.get("verification_event_id"):
        kwargs["verification_event_id"] = evidence["verification_event_id"]
    db.update_finding(finding_id, **kwargs)

    db.log_event(story_key, old.get("stage", ""), "finding_status_changed", {
        "finding_id": finding_id,
        "from": old_status,
        "to": status,
        "reason": reason,
        "evidence": evidence,
    })


def record_verification(
    story_key: str,
    stage: str,
    commands: list[dict],
    covered_findings: list[str] | None = None,
    commit: str | None = None,
) -> None:
    """Write verification_result event."""
    db.log_event(story_key, stage, "verification_result", {
        "commands": commands,
        "covered_findings": covered_findings or [],
        "commit": commit,
        "timestamp": datetime.now().isoformat(),
    })


def record_story_intake(story_key: str, source: str, source_id: str, metadata: dict | None = None) -> None:
    """Record story intake event."""
    db.log_event(story_key, "", "story_intake", {
        "source": source,
        "source_id": source_id,
        "timestamp": datetime.now().isoformat(),
        **(metadata or {}),
    })


def build_quality_packet(story_key: str, stage: str, max_items: int = 5) -> str:
    """Build compact Quality Packet for prompt injection."""
    lines = [f"Quality Packet for {story_key}", ""]

    # Open findings
    findings = db.get_open_findings(story_key)
    if findings:
        lines.append("Open Findings:")
        for f in findings[:max_items]:
            lines.append(f"- [{f['severity'].upper()}] {f['category']}: {f['description']}")
            if f.get("recommendation"):
                lines.append(f"  Fix: {f['recommendation']}")
        lines.append("")
    else:
        lines.append("Open Findings: none")
        lines.append("")

    # Verification baseline
    events = db.get_recent_quality_events(story_key, ["verification_result"], limit=3) if hasattr(db, "get_recent_quality_events') else []
    if events:
        lines.append("Verification Baseline:")
        for e in events[-max_items:]:
            payload = json.loads(e.get("detail", "{}")) if isinstance(e.get("detail"), str) else e.get("detail", {})
            for cmd in payload.get("commands", []):
                lines.append(f"- {cmd.get('cmd', '?')}: {cmd.get('status', '?')}")
        lines.append("")

    return "\n".join(lines)


def build_quality_checklist(story_key: str, stage: str) -> str:
    """Build compact Quality Checklist for executor task file."""
    findings = db.get_open_findings(story_key)
    if not findings:
        return ""

    lines = ["## Quality Checklist", ""]
    for f in findings[:5]:
        lines.append(f"- [ ] Fix: {f['description']}")
        if f.get("recommendation"):
            lines.append(f"      Approach: {f['recommendation']}")
    lines.append("- [ ] Run: pytest && ruff check src tests")
    lines.append("")
    return "\n".join(lines)
```

Note: The `db.get_recent_quality_events` and `db.log_event` functions may need to be added. Check what exists first.

- [ ] **Step 2: Add missing DB helpers if needed**

Check if `db.log_event` exists. If not, check `db.log_stage` — it may serve the same purpose.

Check if `db.get_recent_quality_events` exists. If not, add it:

```python
def get_recent_quality_events(self, story_key: str, event_types: list[str], limit: int = 50) -> list[dict]:
    placeholders = ",".join("?" * len(event_types))
    rows = self._conn.execute(
        f"SELECT * FROM stage_log WHERE story_key = ? AND action IN ({placeholders}) ORDER BY created_at DESC LIMIT ?",
        [story_key] + event_types + [limit],
    ).fetchall()
    return [dict(r) for r in rows]
```

- [ ] **Step 3: Commit**

```bash
git add src/story_lifecycle/orchestrator/quality.py src/story_lifecycle/db/models.py
git commit -m "feat: add quality.py core flywheel logic

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

### Task 3: Prompt 注入 — Quality Packet + Checklist

**Files:**
- Modify: `src/story_lifecycle/orchestrator/nodes.py`

- [ ] **Step 1: Inject Quality Packet into _render_prompt**

In `_render_prompt`, after the sub-story context injection and before `has_prd`, add:

```python
# Quality Packet injection
quality_section = ""
try:
    from .quality import build_quality_packet, build_quality_checklist
    quality_packet = build_quality_packet(state["story_key"], stage)
    if quality_packet.strip() != f"Quality Packet for {state['story_key']}\n\nOpen Findings: none\n":
        quality_section = f"## Quality Packet\n\n{quality_packet}"
    checklist = build_quality_checklist(state["story_key"], stage)
    # Checklist goes into the template vars, not header
except Exception:
    pass
```

Add `{quality_packet_section}` and `{quality_checklist}` to vars_map:

```python
"{quality_packet_section}": quality_section,
"{quality_checklist}": checklist,
```

- [ ] **Step 2: Commit**

```bash
git add src/story_lifecycle/orchestrator/nodes.py
git commit -m "feat: inject Quality Packet and Checklist into prompts

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

### Task 4: story_intake 事件记录

**Files:**
- Modify: `src/story_lifecycle/orchestrator/service.py`

- [ ] **Step 1: Add story_intake event to create_story_from_source**

After creating the story, add:

```python
# Record story_intake event for quality flywheel
try:
    from .quality import record_story_intake
    record_story_intake(
        story_key=key,
        source=item.source,
        source_id=item.id,
        metadata={"has_prd": bool(prd_path), "item_type": item.item_type},
    )
except Exception:
    pass
```

- [ ] **Step 2: Commit**

```bash
git add src/story_lifecycle/orchestrator/service.py
git commit -m "feat: record story_intake event in create_story_from_source

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

### Task 5: Seed findings + 全量测试

**Files:**
- Modify: `tests/test_quality_flywheel.py`

- [ ] **Step 1: Add comprehensive tests**

```python
def test_quality_packet_format(tmp_path):
    """Quality Packet should format findings compactly."""
    ...

def test_finding_verification_fail_reopens(tmp_path):
    """fixed -> open when verification fails."""
    ...

def test_seed_findings():
    """Record the 5 cross-AI review findings as seed data."""
    from story_lifecycle.orchestrator.quality import record_finding
    # ... record the 5 findings from the design doc
    ...
```

- [ ] **Step 2: Run all tests**

```bash
cd /d/story-lifecycle && python -m pytest tests/ -v && ruff check src/
```

- [ ] **Step 3: Commit**

```bash
git add tests/test_quality_flywheel.py
git commit -m "test: add quality flywheel P0 tests and seed findings

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```
