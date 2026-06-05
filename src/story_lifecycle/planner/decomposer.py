"""Decomposer — Step 2: roadmap phase → Issue drafts via LLM."""

from __future__ import annotations

import json
import logging
from pathlib import Path

from .llm import call_llm_json
from .roadmap import load_roadmap, parse_phases

log = logging.getLogger(__name__)

SYSTEM_PROMPT = """你是一位资深项目经理。你需要将路线图中的一个 Phase 拆解为具体的 GitHub Issue。

要求：
1. 每个功能点拆成一个独立 Issue
2. 复杂功能拆成 design Issue + implementation Issue
3. 每个 Issue 包含：title, body, labels, type, dependencies
4. Issue 的 body 应该足够详细，让开发者可以直接开始工作
5. labels 使用小写英文，如: feature, bug, design, implementation
6. 返回 JSON 格式"""

ISSUE_SCHEMA = """返回 JSON 数组，每个元素格式：
{
  "title": "Issue 标题",
  "body": "Issue 正文（Markdown，包含背景、目标、实现建议、验收标准）",
  "labels": ["feature"],
  "type": "design | implementation | bug-fix | test",
  "dependencies": ["依赖的其他 Issue 标题，没有则为空数组"]
}"""


def decompose_phase(
    phase_number: int | None = None,
    *,
    cwd: str | None = None,
    previous_draft: str | None = None,
    feedback: str | None = None,
) -> list[dict]:
    """Decompose a roadmap phase into Issue drafts.

    Args:
        phase_number: Phase number to decompose. If None, uses the first phase.
        cwd: Working directory.
        previous_draft: Previous issues JSON string for refinement.
        feedback: User feedback on previous draft.

    Returns:
        List of Issue draft dicts.
    """
    root = Path(cwd) if cwd else Path.cwd()

    roadmap_content = load_roadmap(cwd=cwd)
    if not roadmap_content:
        raise FileNotFoundError("No roadmap.md found. Run 'story plan roadmap' first.")

    phases = parse_phases(roadmap_content)
    if not phases:
        raise ValueError("No phases found in roadmap.md")

    if phase_number is not None:
        phase = next((p for p in phases if p["number"] == phase_number), None)
        if not phase:
            raise ValueError(f"Phase {phase_number} not found in roadmap")
    else:
        phase = phases[0]

    templates = _load_issue_templates(root)

    prompt = f"""## 路线图 Phase

{phase["content"]}
"""

    if templates:
        prompt += f"""
## 项目 Issue 模板

{templates}
"""

    if previous_draft and feedback:
        prompt += f"""
## 上一版 Issue 草稿

{previous_draft}

请根据用户反馈修改：{feedback}
"""
    else:
        prompt += """
请将以上 Phase 拆解为 GitHub Issue 列表。

确保：
- 每个 Issue 标题简洁明了
- body 包含足够的上下文和实现建议
- 标签使用英文小写
- 复杂功能先拆 design Issue，再拆 implementation Issue"""

    prompt += f"\n\n{ISSUE_SCHEMA}"

    result = call_llm_json(prompt, system=SYSTEM_PROMPT, temperature=0.2)
    if result is None:
        raise RuntimeError("LLM failed to generate Issue drafts")

    return result if isinstance(result, list) else [result]


def load_issues(*, cwd: str | None = None) -> list[dict] | None:
    """Load existing issues.json. Returns None if not found."""
    root = Path(cwd) if cwd else Path.cwd()
    path = root / ".story" / "planning" / "issues.json"
    if path.is_file():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None
    return None


def _load_issue_templates(root: Path) -> str | None:
    """Load .github/ISSUE_TEMPLATE/ content for LLM context."""
    template_dir = root / ".github" / "ISSUE_TEMPLATE"
    if not template_dir.is_dir():
        return None
    parts = []
    for f in sorted(template_dir.glob("*.md")):
        content = f.read_text(encoding="utf-8").strip()
        if content:
            parts.append(f"### {f.stem}\n{content[:500]}")
    return "\n\n".join(parts) if parts else None
