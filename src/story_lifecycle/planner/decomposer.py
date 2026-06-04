"""Decomposer — Step 2: roadmap phase → Issue drafts via LLM."""

from __future__ import annotations

import json
import logging
from pathlib import Path

from .llm import call_llm_json
from .roadmap import load_roadmap, parse_phases
from .state import update_step

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
) -> list[dict]:
    """Decompose a roadmap phase into Issue drafts.

    Args:
        phase_number: Phase number to decompose. If None, uses the first phase.
        cwd: Working directory.

    Returns:
        List of Issue draft dicts.
    """
    root = Path(cwd) if cwd else Path.cwd()

    roadmap = load_roadmap(cwd=cwd)
    if not roadmap:
        raise FileNotFoundError("No roadmap.md found. Run 'story plan roadmap' first.")

    phases = parse_phases(roadmap)
    if not phases:
        raise ValueError("No phases found in roadmap.md")

    if phase_number is not None:
        phase = next((p for p in phases if p["number"] == phase_number), None)
        if not phase:
            raise ValueError(f"Phase {phase_number} not found in roadmap")
    else:
        phase = phases[0]

    # Load Issue templates if available
    templates = _load_issue_templates(root)

    prompt = f"""## 路线图 Phase

{phase["content"]}
"""

    if templates:
        prompt += f"""
## 项目 Issue 模板

{templates}
"""

    prompt += f"""
请将以上 Phase 拆解为 GitHub Issue 列表。

{ISSUE_SCHEMA}

确保：
- 每个 Issue 标题简洁明了
- body 包含足够的上下文和实现建议
- 标签使用英文小写
- 复杂功能先拆 design Issue，再拆 implementation Issue"""

    result = call_llm_json(prompt, system=SYSTEM_PROMPT, temperature=0.2)
    if result is None:
        raise RuntimeError("LLM failed to generate Issue drafts")

    issues = result if isinstance(result, list) else [result]
    _save_issues(issues, cwd=cwd)
    update_step(
        "step_2",
        {"decomposed_phase": phase["number"], "issues_count": len(issues)},
        cwd=cwd,
    )
    return issues


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


def _save_issues(issues: list[dict], *, cwd: str | None = None) -> Path:
    root = Path(cwd) if cwd else Path.cwd()
    planning_dir = root / ".story" / "planning"
    planning_dir.mkdir(parents=True, exist_ok=True)
    path = planning_dir / "issues.json"
    path.write_text(json.dumps(issues, ensure_ascii=False, indent=2), encoding="utf-8")
    return path
