"""Roadmap generator — Step 1: requirements → roadmap.md via LLM."""

from __future__ import annotations

import logging
from pathlib import Path

from .llm import call_llm

log = logging.getLogger(__name__)

SYSTEM_PROMPT = """你是一位资深技术架构师。根据需求文档，生成一份分阶段的开发路线图。

要求：
1. 将项目拆分为 3-6 个 phase
2. 每个 phase 包含：名称、目标、功能列表、依赖关系、验证标准
3. Phase 1 应该是最小可行产品（MVP）
4. 后续 phase 逐步增加功能
5. 用 Markdown 格式输出"""

ROADMAP_TEMPLATE = """# 开发路线图

{roadmap_content}
"""


def generate_roadmap(
    requirements_path: str | Path | None = None,
    *,
    cwd: str | None = None,
    previous_draft: str | None = None,
    feedback: str | None = None,
) -> str:
    """Generate a phased roadmap from requirements using LLM.

    Args:
        requirements_path: Path to requirements.md. If None, auto-finds .story/planning/requirements.md
        cwd: Working directory. Defaults to cwd.
        previous_draft: Previous roadmap draft for refinement.
        feedback: User feedback on previous draft.

    Returns:
        The roadmap markdown content.
    """
    root = Path(cwd) if cwd else Path.cwd()

    if requirements_path:
        req_file = Path(requirements_path)
    else:
        req_file = root / ".story" / "planning" / "requirements.md"

    if not req_file.is_file():
        raise FileNotFoundError(f"Requirements file not found: {req_file}")

    requirements = req_file.read_text(encoding="utf-8")

    code_summary = _scan_code_structure(root)

    prompt = f"""## 需求文档

{requirements}
"""

    if code_summary:
        prompt += f"""
## 现有代码结构

{code_summary}
"""

    if previous_draft and feedback:
        prompt += f"""
## 上一版路线图草稿

{previous_draft}

请根据用户反馈修改：{feedback}
"""
    else:
        prompt += """
请基于以上需求（和现有代码结构），生成一份分阶段的开发路线图。

格式要求：
- 每个 phase 用 ## Phase N: 名称 格式
- 每个 phase 包含：
  - **目标**: 一句话描述
  - **功能列表**: 用 - 开头的列表
  - **依赖**: 依赖哪些前置 phase
  - **验证标准**: 如何判断这个 phase 完成
"""

    prompt += "\n直接输出 Markdown 格式，不要包含额外的解释。"

    return call_llm(prompt, system=SYSTEM_PROMPT, temperature=0.2)


def load_roadmap(*, cwd: str | None = None) -> str | None:
    """Load existing roadmap.md. Returns None if not found."""
    root = Path(cwd) if cwd else Path.cwd()
    path = root / ".story" / "planning" / "roadmap.md"
    if path.is_file():
        return path.read_text(encoding="utf-8")
    return None


def parse_phases(roadmap: str) -> list[dict]:
    """Parse roadmap markdown into a list of phase dicts.

    Returns:
        [{"name": "Phase 1: MVP", "content": "...", "number": 1}, ...]
    """
    import re

    phases = []
    pattern = re.compile(r"^## (Phase (\d+):.+?)$", re.MULTILINE)
    matches = list(pattern.finditer(roadmap))
    for i, match in enumerate(matches):
        start = match.start()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(roadmap)
        phases.append(
            {
                "name": match.group(1),
                "number": int(match.group(2)),
                "content": roadmap[start:end].strip(),
            }
        )
    return phases


def _scan_code_structure(root: Path) -> str | None:
    """Quick scan of project structure for LLM context."""
    skip = {
        ".git",
        "node_modules",
        ".venv",
        "venv",
        "__pycache__",
        ".tox",
        "dist",
        "build",
        ".story",
    }
    lines = []
    try:
        for item in sorted(root.iterdir()):
            if item.name in skip or item.name.startswith("."):
                continue
            if item.is_dir():
                lines.append(f"  {item.name}/")
            else:
                lines.append(f"  {item.name}")
    except PermissionError:
        return None
    if not lines:
        return None
    return "```\n" + "\n".join(lines[:30]) + "\n```"
