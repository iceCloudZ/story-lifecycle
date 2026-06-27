"""Review knowledge management — pattern recurrence + knowledge base updates."""

import logging
from pathlib import Path

from ...db import models as db

log = logging.getLogger("story-lifecycle.nodes.knowledge")


def _check_pattern_recurrence(
    workspace: str, story_key: str, stage: str, issues: list[dict]
):
    """Check if review issues match any active learned patterns (recurrence detection)."""
    if not issues:
        return

    try:
        patterns = db.get_active_learned_patterns(limit=20)
    except Exception:
        return

    if not patterns:
        return

    recurrences = []

    try:
        from ..semantic import match_pattern_recurrence

        for issue in issues:
            result = match_pattern_recurrence(issue, patterns)
            for m in result["data"].get("matches", []):
                pid = m["pattern_id"]
                pattern_obj = next((p for p in patterns if p["id"] == pid), None)
                if pattern_obj:
                    recurrences.append(
                        {
                            "pattern_id": pid,
                            "pattern": pattern_obj.get("pattern", ""),
                            "confidence": m.get("confidence", "low"),
                            "reasoning": m.get("reasoning", ""),
                            "issue": issue,
                        }
                    )
                else:
                    recurrences.append(
                        {
                            "pattern_id": pid,
                            "pattern": "",
                            "confidence": m.get("confidence", "low"),
                            "reasoning": m.get("reasoning", ""),
                            "issue": issue,
                        }
                    )
    except Exception:
        log.warning("Pattern recurrence check failed, skipping")
        return

    if recurrences:
        db.log_event(
            story_key,
            stage,
            "pattern_recurrence",
            {
                "recurrences": recurrences,
                "count": len(recurrences),
            },
        )


def _update_knowledge(
    workspace: str, story_key: str, stage: str, review: dict, stage_output: dict
):
    """Reviewer maintains Story-level knowledge base."""
    knowledge_dir = Path(workspace) / ".story-knowledge" / story_key
    knowledge_dir.mkdir(parents=True, exist_ok=True)

    # design.md on design stage pass
    if stage == "design" and review.get("quality") == "pass":
        design_file = knowledge_dir / "design.md"
        design_file.write_text(
            f"# 设计要点: {story_key}\n\n"
            f"## 需求概述\n{stage_output.get('summary', '')}\n\n"
            f"## 复杂度\n{stage_output.get('complexity', 'M')}\n\n"
            f"## 技术约束\n{stage_output.get('constraints', '无特殊约束')}",
            encoding="utf-8",
        )

    # Append decisions
    decisions_file = knowledge_dir / "decisions.md"
    if not decisions_file.exists():
        decisions_file.write_text(f"# 决策记录: {story_key}\n", encoding="utf-8")

    with open(decisions_file, "a", encoding="utf-8") as f:
        f.write(f"\n## {stage} 阶段\n")
        f.write(f"- 结论: {review.get('summary', '')}\n")
        f.write(f"- 路径评分: {review.get('trajectory_score', 'N/A')}\n")
        for issue in review.get("issues", []):
            f.write(
                f"- 问题: [{issue.get('severity', '')}] "
                f"{issue.get('description', '')} @ {issue.get('location', '')}\n"
            )

    # Append constraints if found
    constraints = stage_output.get("constraints") or stage_output.get("边界条件")
    if constraints:
        constraints_file = knowledge_dir / "constraints.md"
        existing = (
            constraints_file.read_text(encoding="utf-8")
            if constraints_file.exists()
            else ""
        )
        if str(constraints) not in existing:
            with open(constraints_file, "a", encoding="utf-8") as f:
                f.write(f"\n## {stage} 阶段添加\n{constraints}\n")
