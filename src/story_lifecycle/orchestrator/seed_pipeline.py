"""Quality Flywheel Seed Pipeline — LLM-assisted proposal generation from story artifacts.

Manifest-driven: users declare 2-3 real stories with artifact paths.
LLM analyzes context and generates candidate findings + learned patterns.
Human reviews, then apply writes approved items to DB.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger("story-lifecycle.seed_pipeline")

# -------- constants --------

VALID_SEVERITIES = frozenset({"high", "medium", "low"})
VALID_CATEGORIES = frozenset(
    {
        "field-propagation",
        "error_handling",
        "input_validation",
        "sql_injection",
        "state_leak",
        "missing_log",
        "missing_test",
        "routing",
        "schema_change",
        "cross-service",
        "performance",
        "security",
        "configuration",
        "unknown",
    }
)
VALID_CONFIDENCE = frozenset({"high", "medium", "low"})
VALID_MANIFEST_TYPES = frozenset({"requirement", "bugfix", "cross-module"})
VALID_ARTIFACT_TYPES = frozenset(
    {
        "prd",
        "plan",
        "story_record",
        "bug_report",
        "test_result",
        "review",
    }
)
BROAD_TAGS = frozenset(
    {
        "backend",
        "frontend",
        "all",
        "code",
        "infra",
        "database",
        "api",
        "system",
    }
)

MAX_FINDINGS_PER_STORY = 5
MAX_PATTERNS_PER_STORY = 3
MAX_PATTERN_RULE_CHARS = 500
MAX_CONTEXT_CHARS = 12_000

MAX_ARTIFACT_BYTES: dict[str, int] = {
    "prd": 20_000,
    "plan": 10_000,
    "story_record": 10_000,
    "bug_report": 10_000,
    "test_result": 8_000,
    "review": 10_000,
}
DEFAULT_MAX_ARTIFACT_BYTES = 8_000

PROPOSALS_DIR = ".story/quality-seed/proposals"
REVIEWED_DIR = ".story/quality-seed/reviewed"

REQUIRED_FINDING_FIELDS = frozenset({"description", "severity", "category", "evidence"})
REQUIRED_PATTERN_FIELDS = frozenset({"pattern", "rule", "applies_to", "evidence"})


# -------- manifest loader --------


def load_manifest(raw: dict) -> dict[str, Any]:
    """Validate and normalize a manifest dict. Raises ValueError on invalid input."""
    errors: list[str] = []

    story_key = raw.get("story_key")
    if not story_key or not isinstance(story_key, str) or not story_key.strip():
        errors.append("story_key is required and must be a non-empty string")

    title = raw.get("title", "")
    if not title:
        errors.append("title is required")

    mtype = raw.get("type", "")
    if mtype not in VALID_MANIFEST_TYPES:
        errors.append(
            f"type must be one of {sorted(VALID_MANIFEST_TYPES)}, got: {mtype!r}"
        )

    if not isinstance(raw.get("source_root", ""), str):
        errors.append("source_root must be a string")

    artifacts = raw.get("artifacts")
    if not isinstance(artifacts, list) or len(artifacts) == 0:
        errors.append("artifacts must be a non-empty list")
    else:
        for i, a in enumerate(artifacts):
            if not isinstance(a, dict):
                errors.append(f"artifacts[{i}]: must be a dict")
                continue
            if not a.get("path"):
                errors.append(f"artifacts[{i}]: path is required")
            atype = a.get("type", "")
            if atype not in VALID_ARTIFACT_TYPES:
                errors.append(
                    f"artifacts[{i}]: type must be one of {sorted(VALID_ARTIFACT_TYPES)}, got: {atype!r}"
                )

    known_outcomes = raw.get("known_outcomes", [])
    if not isinstance(known_outcomes, list):
        errors.append("known_outcomes must be a list")

    if errors:
        raise ValueError("\n".join(f"  - {e}" for e in errors))

    return {
        "story_key": story_key.strip(),
        "title": title,
        "type": mtype,
        "source_root": raw.get("source_root", ""),
        "artifacts": artifacts,
        "known_outcomes": known_outcomes,
    }


# -------- artifact loader --------


def load_artifacts(manifest: dict) -> list[dict[str, Any]]:
    """Read all declared artifact files. Raises FileNotFoundError listing every missing path."""
    source_root = manifest.get("source_root", "")
    artifacts = manifest.get("artifacts", [])

    missing: list[str] = []
    loaded: list[dict[str, Any]] = []

    for a in artifacts:
        raw_path = a["path"]
        if os.path.isabs(raw_path):
            path = Path(raw_path)
        else:
            path = Path(source_root) / raw_path

        if not path.exists():
            missing.append(str(path))
            continue

        if not path.is_file():
            missing.append(f"{path} (not a file)")
            continue

        try:
            raw_content = path.read_bytes()
        except Exception:
            missing.append(f"{path} (unreadable)")
            continue

        # Check binary
        try:
            content = raw_content.decode("utf-8")
        except UnicodeDecodeError:
            log.warning(f"Skipping binary file: {path}")
            continue

        max_bytes = MAX_ARTIFACT_BYTES.get(a["type"], DEFAULT_MAX_ARTIFACT_BYTES)
        truncated = len(raw_content) > max_bytes
        if truncated:
            content = content[:max_bytes]

        if len(content.strip()) == 0:
            content = "(empty file)"
            truncated = False

        loaded.append(
            {
                "path": str(path),
                "type": a["type"],
                "content": content,
                "truncated": truncated,
            }
        )

    if missing:
        msg = "Missing artifact files:\n" + "\n".join(f"  - {p}" for p in missing)
        raise FileNotFoundError(msg)

    return loaded


# -------- context summarizer --------


def _summarize_prd(content: str) -> str:
    lines = []
    in_header = False
    for line in content.splitlines()[:80]:
        s = line.strip()
        if s.startswith("#"):
            in_header = True
            lines.append(s)
        elif in_header and s:
            lines.append(s[:300])
    if not lines:
        lines.append(content[:1500])
    return "\n".join(lines)


def _summarize_plan(content: str) -> str:
    markers = [
        "实现路径",
        "实现方案",
        "风险",
        "测试计划",
        "Implementation",
        "Risk",
        "Test Plan",
    ]
    found: list[str] = []
    for line in content.splitlines():
        for m in markers:
            if m in line:
                found.append(line.strip()[:300])
    if not found:
        found.append(content[:1500])
    return "\n".join(found)


def _summarize_story_record(content: str) -> str:
    try:
        data = json.loads(content)
        parts = []
        for k in ("current_stage", "status", "profile"):
            if k in data:
                parts.append(f"{k}: {data[k]}")
        events = data.get("events", [])
        if events:
            parts.append(f"events: {len(events)} total")
            for e in events[-5:]:
                parts.append(
                    f"  [{e.get('event_type', '?')}] {e.get('stage', '?')}: {str(e.get('payload', ''))[:150]}"
                )
        return "\n".join(parts) if parts else content[:1500]
    except (json.JSONDecodeError, TypeError):
        return content[:1500]


def _summarize_bug_report(content: str) -> str:
    markers = [
        "现象",
        "根因",
        "修复",
        "验证",
        "Symptom",
        "Root Cause",
        "Fix",
        "Verification",
    ]
    found: list[str] = []
    for line in content.splitlines():
        for m in markers:
            if m in line:
                found.append(line.strip()[:300])
    if not found:
        found.append(content[:1500])
    return "\n".join(found)


def _summarize_test_result(content: str) -> str:
    lines = content.splitlines()
    head = "\n".join(lines[:30])
    if len(lines) > 30:
        head += f"\n... ({len(lines)} total lines)"
    return head


def _summarize_review(content: str) -> str:
    markers = ["quality", "issues", "suggestions", "评分", "Review", "问题"]
    found: list[str] = []
    for line in content.splitlines():
        for m in markers:
            if m.lower() in line.lower():
                found.append(line.strip()[:300])
    if not found:
        found.append(content[:1500])
    return "\n".join(found)


_SUMMARIZERS = {
    "prd": _summarize_prd,
    "plan": _summarize_plan,
    "story_record": _summarize_story_record,
    "bug_report": _summarize_bug_report,
    "test_result": _summarize_test_result,
    "review": _summarize_review,
}


def summarize_context(artifacts: list[dict], manifest: dict) -> str:
    """Compress loaded artifacts into a compact text block for LLM consumption."""
    sections: list[str] = []
    for a in artifacts:
        summarizer = _SUMMARIZERS.get(a["type"], lambda c: c[:1500])
        try:
            summary = summarizer(a["content"])
        except Exception:
            summary = a["content"][:1500]
        truncated_mark = " (truncated)" if a.get("truncated") else ""
        sections.append(f"### {a['type']}: {a['path']}{truncated_mark}\n{summary}")

    # Known outcomes
    outcomes = manifest.get("known_outcomes", [])
    if outcomes:
        sections.append("### known_outcomes\n" + "\n".join(f"- {o}" for o in outcomes))

    full = "\n\n---\n\n".join(sections)

    if len(full) > MAX_CONTEXT_CHARS:
        full = full[: MAX_CONTEXT_CHARS - 100] + "\n\n... (context truncated)"

    return full


# -------- LLM seed analyst --------


_ANALYST_PROMPT = """你是 Quality Flywheel 的种子数据分析师。你的任务是分析一个真实 story 的上下文，提炼出可复用的质量发现（findings）和学习规则（learned patterns）。

## Story 信息

- **Story Key**: {story_key}
- **标题**: {title}
- **类型**: {story_type}

## Story 上下文

{context}

## Quality Flywheel Schema

### Finding（发现）
记录一个从历史 story 中学到的具体质量问题。

字段:
- severity: "high" | "medium" | "low"
- category: 从允许列表中选择（{categories}）
- description: 一段话描述问题和影响
- location: 文件或模块位置（可选）
- root_cause: 根因分析（可选）
- recommendation: 修复或预防建议（可选）
- evidence: 证据列表（必填），每条证据是一个文件路径或引用
- confidence: "high" | "medium" | "low"

### Learned Pattern（学习规则）
从多个 findings 提炼的可执行规则。

字段:
- pattern: 简短名称
- applies_to: 适用范围标签列表（必至少 2 个标签，且不能全是宽泛标签）
- rule: 可执行的检查规则（1-3 句话）
- evidence: 证据来源列表（必填）
- confidence: "high" | "medium" | "low"

## 严格约束

1. 最多生成 {max_findings} 条 findings 和 {max_patterns} 条 patterns
2. 每条 finding 和 pattern 必须有至少 1 条 evidence
3. applies_to 必须有至少 2 个标签，且至少 1 个不是宽泛标签
4. 以下标签为**宽泛标签**，不能单独出现或全由它们组成 applies_to: {broad_tags}
5. severity 必须是 high/medium/low 之一
6. category 必须从允许列表中选择，不确定时用 "unknown"
7. 每条 pattern 的 rule 必须可执行、可检查
8. 只基于 evidence 输出，不确定时降低 confidence
9. 不要把一次性业务结论伪装成通用规则

## 输出格式

严格返回 JSON（不要 markdown 包裹）:

```json
{{
  "story_key": "{story_key}",
  "summary": "1-2 句话概述该 story 的质量要点",
  "risk_tags": ["标签1", "标签2"],
  "proposed_findings": [
    {{
      "severity": "medium",
      "category": "field-propagation",
      "location": "prd/1065520.md",
      "description": "...",
      "root_cause": "...",
      "recommendation": "...",
      "evidence": ["prd/1065520.md#section"],
      "confidence": "medium"
    }}
  ],
  "proposed_patterns": [
    {{
      "pattern": "...",
      "applies_to": ["promotion", "hc-order"],
      "rule": "...",
      "evidence": ["prd/1065520.md"],
      "confidence": "medium"
    }}
  ],
  "review_questions": [
    "人工审核时需要确认的问题?"
  ]
}}
```"""


def run_llm_analysis(
    manifest: dict,
    context: str,
    *,
    api_key: str,
    base_url: str,
    model: str,
) -> dict:
    """Call LLM to analyze story context and generate proposed findings and patterns."""
    from . import planner

    prompt = _ANALYST_PROMPT.format(
        story_key=manifest["story_key"],
        title=manifest["title"],
        story_type=manifest["type"],
        context=context,
        categories=", ".join(sorted(VALID_CATEGORIES)),
        max_findings=MAX_FINDINGS_PER_STORY,
        max_patterns=MAX_PATTERNS_PER_STORY,
        broad_tags=", ".join(sorted(BROAD_TAGS)),
    )

    try:
        result = planner._call_llm(
            base_url,
            api_key,
            model,
            prompt,
            story_key=manifest["story_key"],
            stage="seed_analysis",
        )
        return result
    except Exception as e:
        msg = str(e)
        if "JSON" in msg or "json" in msg or "parse" in msg.lower():
            raise ValueError(f"LLM returned invalid JSON: {msg[:400]}")
        raise RuntimeError(f"LLM analysis failed: {msg[:400]}")


# -------- schema validator --------


def _validate_finding(item: dict, idx: int) -> tuple[dict | None, list[str]]:
    warnings: list[str] = []
    if not item.get("description"):
        warnings.append(f"finding[{idx}]: missing description, rejected")
        return None, warnings

    severity = item.get("severity", "medium")
    if severity not in VALID_SEVERITIES:
        warnings.append(
            f"finding[{idx}]: invalid severity {severity!r}, defaulted to 'medium'"
        )
        severity = "medium"
    item["severity"] = severity

    category = item.get("category", "unknown")
    if category not in VALID_CATEGORIES:
        warnings.append(
            f"finding[{idx}]: unknown category {category!r}, set to 'unknown'"
        )
        category = "unknown"
    item["category"] = category

    evidence = item.get("evidence", [])
    if not evidence or not isinstance(evidence, list) or len(evidence) == 0:
        warnings.append(f"finding[{idx}]: missing evidence, rejected")
        return None, warnings

    confidence = item.get("confidence", "medium")
    if confidence not in VALID_CONFIDENCE:
        warnings.append(
            f"finding[{idx}]: invalid confidence {confidence!r}, defaulted to 'medium'"
        )
        confidence = "medium"
    item["confidence"] = confidence

    # Set defaults for optional fields
    item.setdefault("location", "")
    item.setdefault("root_cause", "")
    item.setdefault("recommendation", "")

    return item, warnings


def _validate_pattern(item: dict, idx: int) -> tuple[dict | None, list[str]]:
    warnings: list[str] = []
    if not item.get("pattern"):
        warnings.append(f"pattern[{idx}]: missing pattern name, rejected")
        return None, warnings

    rule = item.get("rule", "")
    if not rule:
        warnings.append(f"pattern[{idx}]: missing rule, rejected")
        return None, warnings
    if len(rule) > MAX_PATTERN_RULE_CHARS:
        item["rule"] = rule[: MAX_PATTERN_RULE_CHARS - 20] + "... (truncated)"
        warnings.append(
            f"pattern[{idx}]: rule truncated ({len(rule)} → {MAX_PATTERN_RULE_CHARS} chars)"
        )

    applies_to = item.get("applies_to", [])
    if not isinstance(applies_to, list) or len(applies_to) < 2:
        warnings.append(
            f"pattern[{idx}]: applies_to must have >= 2 tags, got {len(applies_to) if isinstance(applies_to, list) else 'non-list'}, rejected"
        )
        return None, warnings

    non_broad = [t for t in applies_to if t.lower() not in BROAD_TAGS]
    if not non_broad:
        warnings.append(
            f"pattern[{idx}]: all applies_to tags are broad ({applies_to}), rejected"
        )
        return None, warnings

    evidence = item.get("evidence", [])
    if not evidence or not isinstance(evidence, list) or len(evidence) == 0:
        warnings.append(f"pattern[{idx}]: missing evidence, rejected")
        return None, warnings

    confidence = item.get("confidence", "medium")
    if confidence not in VALID_CONFIDENCE:
        warnings.append(
            f"pattern[{idx}]: invalid confidence {confidence!r}, defaulted to 'medium'"
        )
        confidence = "medium"
    item["confidence"] = confidence

    return item, warnings


def validate_proposal(llm_output: dict, manifest: dict) -> tuple[dict, list[str]]:
    """Validate LLM output against schema rules. Never raises — returns sanitized + warnings."""
    warnings: list[str] = []

    # story_key match
    if llm_output.get("story_key") != manifest["story_key"]:
        warnings.append(
            f"story_key mismatch: expected {manifest['story_key']}, got {llm_output.get('story_key')}"
        )
        llm_output["story_key"] = manifest["story_key"]

    # Validate findings
    findings = llm_output.get("proposed_findings", [])
    if not isinstance(findings, list):
        findings = []
    valid_findings: list[dict] = []
    for i, f in enumerate(findings):
        if not isinstance(f, dict):
            warnings.append(f"finding[{i}]: not a dict, skipped")
            continue
        validated, warns = _validate_finding(dict(f), i)
        warnings.extend(warns)
        if validated is not None:
            valid_findings.append(validated)

    if len(valid_findings) > MAX_FINDINGS_PER_STORY:
        removed = valid_findings[MAX_FINDINGS_PER_STORY:]
        valid_findings = valid_findings[:MAX_FINDINGS_PER_STORY]
        warnings.append(
            f"findings truncated: {len(removed)} items beyond limit of {MAX_FINDINGS_PER_STORY} removed"
        )

    # Validate patterns
    patterns = llm_output.get("proposed_patterns", [])
    if not isinstance(patterns, list):
        patterns = []
    valid_patterns: list[dict] = []
    seen_names: set[str] = set()
    for i, p in enumerate(patterns):
        if not isinstance(p, dict):
            warnings.append(f"pattern[{i}]: not a dict, skipped")
            continue
        validated, warns = _validate_pattern(dict(p), i)
        warnings.extend(warns)
        if validated is not None:
            name = validated.get("pattern", "")
            if name in seen_names:
                warnings.append(f"pattern[{i}]: duplicate name '{name}', skipped")
                continue
            seen_names.add(name)
            valid_patterns.append(validated)

    if len(valid_patterns) > MAX_PATTERNS_PER_STORY:
        removed = valid_patterns[MAX_PATTERNS_PER_STORY:]
        valid_patterns = valid_patterns[:MAX_PATTERNS_PER_STORY]
        warnings.append(
            f"patterns truncated: {len(removed)} items beyond limit of {MAX_PATTERNS_PER_STORY} removed"
        )

    return {
        "story_key": llm_output.get("story_key", manifest["story_key"]),
        "summary": llm_output.get("summary", ""),
        "risk_tags": llm_output.get("risk_tags", []),
        "proposed_findings": valid_findings,
        "proposed_patterns": valid_patterns,
        "review_questions": llm_output.get("review_questions", []),
    }, warnings


# -------- review queue --------


def write_proposal(
    proposal: dict,
    manifest: dict,
    workspace: str,
    dry_run: bool = True,
) -> Path | None:
    """Write validated proposal to review queue. dry_run=True prints to stdout only."""
    proposal_doc = {
        "manifest": {
            "story_key": manifest["story_key"],
            "title": manifest["title"],
            "type": manifest["type"],
            "source_root": manifest.get("source_root", ""),
        },
        "analysis": {
            "summary": proposal.get("summary", ""),
            "risk_tags": proposal.get("risk_tags", []),
        },
        "proposed_findings": proposal.get("proposed_findings", []),
        "proposed_patterns": proposal.get("proposed_patterns", []),
        "review_questions": proposal.get("review_questions", []),
        "review_status": {
            "findings_approved": [],
            "findings_rejected": [],
            "patterns_approved": [],
            "patterns_rejected": [],
            "reviewer_notes": "",
            "reviewed_at": None,
        },
        "pipeline_meta": {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "validator_warnings": proposal.get("_warnings", []),
            "artifact_count": len(manifest.get("artifacts", [])),
        },
    }

    if dry_run:
        from rich.console import Console

        console = Console()
        console.print()
        console.rule("[bold cyan]Seed Proposal (dry-run)")
        console.print_json(json.dumps(proposal_doc, ensure_ascii=False, indent=2))
        warnings_list = proposal.get("_warnings", [])
        if warnings_list:
            console.print()
            console.print("[yellow]Validation warnings:[/]")
            for w in warnings_list:
                console.print(f"  [yellow]- {w}[/]")
        return None

    proposals_dir = Path(workspace) / PROPOSALS_DIR
    proposals_dir.mkdir(parents=True, exist_ok=True)
    filepath = proposals_dir / f"{manifest['story_key']}.json"
    filepath.write_text(
        json.dumps(proposal_doc, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return filepath


def load_reviewed_proposal(filepath: str) -> dict:
    """Load a reviewed proposal JSON. Raises ValueError if structure invalid or unreviewed."""
    path = Path(filepath)
    if not path.exists():
        raise ValueError(f"File not found: {filepath}")

    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise ValueError(f"Invalid JSON in {filepath}: {e}")

    if "manifest" not in doc:
        raise ValueError("Missing 'manifest' key")
    sk = doc["manifest"].get("story_key")
    if not sk:
        raise ValueError("Missing 'manifest.story_key'")

    review = doc.get("review_status")
    if not review:
        raise ValueError("Missing 'review_status' key. File has not been reviewed.")
    if review.get("reviewed_at") is None:
        raise ValueError(
            "Proposal has not been reviewed. Set review_status.reviewed_at before applying."
        )

    return doc


# -------- apply --------


def apply_reviewed(proposal: dict) -> dict:
    """Apply a reviewed proposal: write approved findings and patterns to DB.

    Patterns are written as 'proposed' — never auto-approved or auto-activated.
    """
    from . import quality

    story_key = proposal["manifest"]["story_key"]
    findings_approved: list[int] = proposal["review_status"].get(
        "findings_approved", []
    )
    patterns_approved: list[int] = proposal["review_status"].get(
        "patterns_approved", []
    )
    proposed_findings: list[dict] = proposal.get("proposed_findings", [])
    proposed_patterns: list[dict] = proposal.get("proposed_patterns", [])

    findings_written = 0
    patterns_written = 0
    errors: list[str] = []

    for idx in findings_approved:
        if idx < 0 or idx >= len(proposed_findings):
            errors.append(
                f"Finding index {idx} out of range (0-{len(proposed_findings) - 1})"
            )
            continue
        f = proposed_findings[idx]
        try:
            evidence_list = f.get("evidence", [])
            confidence = f.get("confidence", "medium")
            # Embed evidence + confidence in root_cause for DB auditability
            root_cause = f.get("root_cause", "")
            if evidence_list:
                evidence_note = "Evidence: " + ", ".join(evidence_list)
                root_cause = (
                    f"{root_cause}\n{evidence_note}" if root_cause else evidence_note
                )
            fid = quality.record_finding(
                story_key=story_key,
                stage="seed_analysis",
                finding={
                    "source": "seed_pipeline",
                    "severity": f["severity"],
                    "category": f["category"],
                    "description": f["description"],
                    "location": f.get("location", ""),
                    "recommendation": f.get("recommendation", ""),
                    "root_cause": root_cause,
                    "evidence": evidence_list,
                    "confidence": confidence,
                },
            )
            # Apply target status if specified
            target_status = f.get("target_status", "open")
            if target_status == "verified":
                verification_evidence = f.get("verification_evidence")
                if not verification_evidence:
                    errors.append(
                        f"Finding[{idx}]: target_status=verified but missing "
                        "verification_evidence — status left as 'open'"
                    )
                else:
                    quality.update_finding_status(
                        story_key,
                        fid,
                        "verified",
                        reason="Seed from historical story with verification evidence",
                        evidence={"verification_event_id": verification_evidence},
                    )
            findings_written += 1
        except Exception as exc:
            errors.append(f"Finding[{idx}]: write failed: {exc}")

    for idx in patterns_approved:
        if idx < 0 or idx >= len(proposed_patterns):
            errors.append(
                f"Pattern index {idx} out of range (0-{len(proposed_patterns) - 1})"
            )
            continue
        p = proposed_patterns[idx]
        try:
            evidence_list = p.get("evidence", [])
            pid = quality.propose_learned_pattern(
                story_key=story_key,
                pattern=p["pattern"],
                applies_to=p["applies_to"],
                rule=p["rule"],
                source_findings=[],
                confidence=p.get("confidence", "medium"),
            )
            # Preserve evidence chain in event log
            if evidence_list or p.get("confidence"):
                try:
                    from ..db import models as db

                    db.log_event(
                        story_key,
                        "seed_analysis",
                        "seed_pattern_evidence",
                        {
                            "pattern_id": pid,
                            "evidence": evidence_list,
                            "confidence": p.get("confidence", "medium"),
                        },
                    )
                except Exception:
                    pass
            patterns_written += 1
        except Exception as exc:
            errors.append(f"Pattern[{idx}]: write failed: {exc}")

    # Log apply event
    try:
        from ..db import models as db

        db.log_event(
            story_key,
            "seed_analysis",
            "seed_pipeline_applied",
            {
                "findings_written": findings_written,
                "patterns_written": patterns_written,
                "errors": errors,
            },
        )
    except Exception:
        pass

    return {
        "findings_written": findings_written,
        "patterns_written": patterns_written,
        "errors": errors,
    }
