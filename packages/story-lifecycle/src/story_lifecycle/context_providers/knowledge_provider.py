"""Knowledge context provider — injects mined outcome/process knowledge into prompts.

Reads phase-1/phase-2 mining artifacts from story-miner and returns a short
markdown summary for the story's task_type. The provider is intentionally
lenient: missing artifacts or mismatched story keys result in ``None`` so
prompt rendering is never blocked.
"""
from __future__ import annotations

import json
import os
from pathlib import Path


class KnowledgeContextProvider:
    """Provide mined knowledge context for a story/stage."""

    def __init__(self, config: dict | None = None):
        self.config = config or {}
        # Default to story-miner output directory
        self.base = Path(
            self.config.get("base_path")
            or os.environ.get("STORY_MINER_OUT")
            or "packages/story-miner/scripts/out"
        )

    def _load(self, name: str) -> dict | list | None:
        path = self.base / name
        if not path.exists():
            return None
        try:
            with path.open("r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return None

    def _task_type_for(self, story_key: str) -> str | None:
        data = self._load("story_task_types.json")
        if not isinstance(data, list):
            return None
        for r in data:
            if r.get("story_key") == story_key:
                return r.get("task_type")
        return None

    def get_context(self, story_key: str, workspace: str, stage: str) -> str | None:
        """Return markdown knowledge context for this story, or None."""
        task_type = self._task_type_for(story_key)
        if not task_type:
            return None

        phase2 = self._load("result_axis_phase2.json") or {}
        graph = self._load("bug_story_graph.json") or {}

        lines = [f"## 飞轮知识上下文（task_type={task_type}）\n"]

        # 1. bug-prone 文件（仅取该类 top 5）
        patterns = (phase2.get("patterns_by_task_type") or {}).get(task_type, [])
        if patterns:
            lines.append("### 历史高风险文件\n")
            lines.append("以下文件在本类任务中反复改动且关联 bug 最多，设计/实现时建议重点 review：\n")
            for p in patterns[:5]:
                lines.append(f"- `{p['file']}` (commits={p['commit_count']}, bug_weight={p['bug_weight']})")
            lines.append("")

        # 2. cycle-time 基线
        ct = (phase2.get("cycle_time") or {}).get("by_task_type", {}).get(task_type)
        if ct and ct.get("n"):
            lines.append("### 同类 bug 修复耗时基线\n")
            lines.append(
                f"- median={ct['median']}h, p90={ct['p90']}h, mean={ct['mean']}h (n={ct['n']})\n"
            )

        # 3. 该类 top bug 磁铁（警示）
        stories = graph.get("stories", [])
        type_stories = [
            s for s in stories
            if self._task_type_for(s.get("story_key", "")) == task_type and s.get("bug_count", 0) > 0
        ]
        type_stories.sort(key=lambda s: -s["bug_count"])
        if type_stories[:3]:
            lines.append("### 本类历史 bug 磁铁\n")
            for s in type_stories[:3]:
                lines.append(f"- `{s['story_key']}` {s['title'][:50]} (bugs={s['bug_count']})")
            lines.append("")

        # 4. 阶段-specific 提示
        if stage in ("design", "build"):
            lines.append("### 设计/实现建议\n")
            lines.append("- 若改动涉及上述高风险文件，请在 research.md 中显式评估回归风险。\n")
            lines.append("- 若需求与历史 bug 磁铁业务相似，优先复用已验证的分支/模块模式。\n")
        elif stage == "verify":
            lines.append("### 验证建议\n")
            lines.append("- 针对上述高风险文件补充回归检查；若出现同类 bug，gate 应 block。\n")

        return "\n".join(lines)
