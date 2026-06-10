"""Project Intelligence Probe — read-only agent probe with validation.

Implements the probe portion of the Workspace Onboarding design:
- Probe task builder (read-only prompt construction)
- Destructive command rejection
- Evidence validation
- Output schema validation
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

# Patterns that indicate destructive commands — rejected in probe output
_DESTRUCTIVE_PATTERNS = [
    r"rm\s+-rf",
    r"rm\s+-r\s",
    r"git\s+reset\s+--hard",
    r"git\s+checkout\s+\.",
    r"git\s+clean",
    r"del\s+/s",
    r"Remove-Item\s+-Recurse",
    r"drop\s+table",
    r"truncate\s+table",
    r"DELETE\s+FROM\s+\w+\s*;?\s*$",
    r"format\s+[A-Z]:",
    r"shutil\.rmtree",
    r"os\.remove",
    r"subprocess.*rm\s",
]


def build_probe_prompt(workspace: str | Path, question: str) -> str:
    """Build a read-only probe task for the agent."""
    ws = str(workspace)
    return f"""你是 Project Intelligence Probe。

任务：{question}

约束：
- 只读，不要修改任何文件。
- 不要安装依赖。
- 不要切换 git 分支。
- 不要运行耗时测试。
- 只允许读取 workspace 下的 README、配置、脚本、CI 文件。
- workspace 路径: {ws}

输出 raw JSON（不要 markdown 包裹）：
{{
  "facts": [
    {{
      "type": "string (e.g. test_command, build_command, deploy_rule)",
      "value": "string",
      "evidence": [{{ "path": "string", "kind": "string" }}]
    }}
  ],
  "hypotheses": [
    {{
      "type": "string",
      "value": "string",
      "confidence": 0.0-1.0,
      "evidence": [{{ "path": "string", "kind": "string" }}]
    }}
  ],
  "open_questions": ["string"]
}}
"""


def validate_probe_output(raw: str, workspace: str | Path) -> dict[str, Any]:
    """Validate probe output and return structured result.

    Returns a dict with:
    - valid: bool
    - facts: list of valid facts
    - hypotheses: list of valid hypotheses
    - open_questions: list
    - rejected: list of rejected items with reasons
    """
    ws = Path(workspace).resolve()
    rejected: list[dict[str, str]] = []
    valid_facts: list[dict[str, Any]] = []
    valid_hypotheses: list[dict[str, Any]] = []

    # Parse JSON
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        # Try to extract JSON from markdown-wrapped output
        match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", raw, re.DOTALL)
        if match:
            try:
                data = json.loads(match.group(1))
            except json.JSONDecodeError:
                return {
                    "valid": False,
                    "error": "output is not valid JSON",
                    "rejected": rejected,
                }
        else:
            return {
                "valid": False,
                "error": "output is not valid JSON",
                "rejected": rejected,
            }

    if not isinstance(data, dict):
        return {
            "valid": False,
            "error": "output is not a JSON object",
            "rejected": rejected,
        }

    # Validate facts
    for fact in data.get("facts", []):
        if not isinstance(fact, dict):
            rejected.append({"item": str(fact), "reason": "not a dict"})
            continue

        if not fact.get("type") or not fact.get("value"):
            rejected.append({"item": str(fact), "reason": "missing type or value"})
            continue

        # Evidence validation
        evidence = fact.get("evidence", [])
        if not evidence:
            rejected.append({"item": str(fact), "reason": "no evidence"})
            continue

        valid_evidence = []
        for ev in evidence:
            if not isinstance(ev, dict) or not ev.get("path"):
                continue
            ev_path = ws / ev["path"]
            # Check path is within workspace
            try:
                ev_path.resolve().relative_to(ws)
            except ValueError:
                rejected.append(
                    {
                        "item": ev["path"],
                        "reason": "evidence path outside workspace",
                    }
                )
                continue
            if not ev_path.exists():
                rejected.append(
                    {"item": ev["path"], "reason": "evidence path does not exist"}
                )
                continue
            valid_evidence.append(ev)

        if not valid_evidence:
            rejected.append({"item": str(fact), "reason": "no valid evidence"})
            continue

        fact["evidence"] = valid_evidence

        # Destructive command check
        value = fact.get("value", "")
        if _contains_destructive_pattern(value):
            rejected.append(
                {"item": str(fact), "reason": "destructive command pattern"}
            )
            continue

        valid_facts.append(fact)

    # Validate hypotheses
    for hyp in data.get("hypotheses", []):
        if not isinstance(hyp, dict):
            rejected.append({"item": str(hyp), "reason": "not a dict"})
            continue

        if not hyp.get("type") or not hyp.get("value"):
            rejected.append({"item": str(hyp), "reason": "missing type or value"})
            continue

        conf = hyp.get("confidence", 0.5)
        if not isinstance(conf, (int, float)) or conf < 0 or conf > 1:
            hyp["confidence"] = max(0.0, min(1.0, float(conf) if conf else 0.5))

        valid_hypotheses.append(hyp)

    open_questions = [
        q for q in data.get("open_questions", []) if isinstance(q, str) and q.strip()
    ]

    return {
        "valid": True,
        "facts": valid_facts,
        "hypotheses": valid_hypotheses,
        "open_questions": open_questions,
        "rejected": rejected,
    }


def _contains_destructive_pattern(value: str) -> bool:
    """Check if a string contains destructive command patterns."""
    for pattern in _DESTRUCTIVE_PATTERNS:
        if re.search(pattern, value, re.IGNORECASE):
            return True
    return False


def run_probe(workspace: str | Path, question: str) -> dict[str, Any]:
    """Execute a probe using the configured LLM.

    Returns a result dict with facts, hypotheses, open_questions, or error.
    """
    from ..llm_client import get_llm

    prompt = build_probe_prompt(workspace, question)

    try:
        response = get_llm().invoke(prompt, temperature=0.2, timeout=120)
    except Exception as e:
        return {"error": str(e), "facts": [], "hypotheses": [], "open_questions": []}

    if not response:
        return {
            "error": "empty LLM response",
            "facts": [],
            "hypotheses": [],
            "open_questions": [],
        }

    validated = validate_probe_output(response, workspace)

    if not validated.get("valid"):
        return {
            "error": validated.get("error", "validation failed"),
            "facts": [],
            "hypotheses": [],
            "open_questions": [],
            "rejected": validated.get("rejected", []),
        }

    return {
        "facts": validated["facts"],
        "hypotheses": validated["hypotheses"],
        "open_questions": validated["open_questions"],
        "rejected": validated.get("rejected", []),
    }
