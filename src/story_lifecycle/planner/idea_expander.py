"""Idea expander — Step 0a: idea → requirements.md via LLM dialog."""

from __future__ import annotations

import logging
from pathlib import Path

from .llm import call_llm

log = logging.getLogger(__name__)

SYSTEM_PROMPT = """你是一位资深产品经理。用户有一个项目 idea，你需要通过对话帮助他澄清需求，最终生成一份结构化的需求文档。

要求：
1. 先问 3-5 个关键问题（目标用户、核心功能、技术偏好、约束条件）
2. 每次只问 1-2 个问题，等用户回答后再继续
3. 收集够信息后，生成 requirements.md 格式的需求文档
4. 需求文档包含：项目概述、目标用户、核心功能列表、非功能性需求、技术约束"""

_SKIP_DIRS = {
    ".git",
    "node_modules",
    ".venv",
    "venv",
    "__pycache__",
    ".tox",
    "dist",
    "build",
    ".story",
    ".mypy_cache",
    ".pytest_cache",
    ".idea",
    ".vscode",
    "target",
    "vendor",
    "bundle",
}


def expand_idea_to_requirements(
    idea: str,
    *,
    cwd: str | None = None,
    max_rounds: int = 5,
) -> str:
    """Expand an idea into requirements via LLM. Returns the requirements markdown."""
    prompt = f"""用户的 idea：{idea}

请基于这个 idea，生成一份完整的需求文档（中文）。包含：
1. 项目概述（一段话描述项目目标和价值）
2. 目标用户（主要用户群体和使用场景）
3. 核心功能列表（每个功能一行，用 - 开头，按优先级排序）
4. 非功能性需求（性能、安全、可用性等）
5. 技术约束（如果有明显的技术选型要求）

直接输出 Markdown 格式的需求文档，不要包含额外的解释。"""

    content = call_llm(prompt, system=SYSTEM_PROMPT, temperature=0.3)
    return content


def analyze_codebase_to_requirements(
    *,
    cwd: str | None = None,
    previous_draft: str | None = None,
    feedback: str | None = None,
) -> str:
    """Analyze existing codebase and generate requirements.md via LLM.

    For projects that already have code — scans the structure, reads key files,
    and uses LLM to understand what the project does and generate requirements.
    """
    root = Path(cwd) if cwd else Path.cwd()

    context_parts = []

    # 1. Directory structure
    tree = _scan_tree(root)
    if tree:
        context_parts.append(f"## 目录结构\n```\n{tree}\n```")

    # 2. README
    for name in ("README.md", "README.rst", "README.txt", "README"):
        readme = root / name
        if readme.is_file():
            content = readme.read_text(encoding="utf-8", errors="replace")[:3000]
            context_parts.append(f"## README\n{content}")
            break

    # 3. Project metadata (pyproject.toml / package.json / etc)
    for name in ("pyproject.toml", "package.json", "Cargo.toml", "go.mod"):
        meta = root / name
        if meta.is_file():
            content = meta.read_text(encoding="utf-8", errors="replace")[:2000]
            context_parts.append(f"## {name}\n```\n{content}\n```")
            break

    # 4. Key source files (entry points)
    key_files = _find_key_files(root)
    for path, content in key_files:
        context_parts.append(f"## {path}\n```\n{content[:2000]}\n```")

    if not context_parts:
        raise RuntimeError("无法分析项目：未找到可识别的代码或文档")

    project_context = "\n\n".join(context_parts)

    prompt = f"""## 项目代码

{project_context}

---

请分析以上代码，理解这个项目是做什么的，然后生成一份完整的需求文档（中文）。包含：
1. 项目概述（一段话描述项目目标和价值）
2. 目标用户（主要用户群体和使用场景）
3. 核心功能列表（每个功能一行，用 - 开头，按优先级排序）— 基于已有代码推断已实现的功能
4. 非功能性需求（性能、安全、可用性等）
5. 技术约束（基于现有技术栈）
6. 已完成功能 vs 待开发功能的判断（如果能从代码推断）"""

    if previous_draft and feedback:
        prompt += f"""

以下是上一版草稿：
{previous_draft}

请根据用户反馈修改：{feedback}"""

    prompt += "\n\n直接输出 Markdown 格式的需求文档，不要包含额外的解释。"

    content = call_llm(prompt, system=SYSTEM_PROMPT, temperature=0.2)
    return content


def start_idea_dialog() -> list[dict]:
    """Get the first round of clarifying questions for the user's idea."""
    prompt = """用户要开始一个新项目。请提出 3 个最关键的澄清问题，帮助理解项目需求。

每个问题格式：
{"question": "问题内容", "field": "字段名"}

返回 JSON 数组。"""

    from .llm import call_llm_json

    result = call_llm_json(prompt, system=SYSTEM_PROMPT, temperature=0.3)
    if isinstance(result, list):
        return result
    return [{"question": "请描述你的项目 idea", "field": "idea"}]


def _scan_tree(root: Path, max_lines: int = 80) -> str | None:
    lines = []
    try:
        for item in sorted(root.iterdir()):
            if item.name in _SKIP_DIRS or item.name.startswith("."):
                continue
            if item.is_dir():
                lines.append(f"{item.name}/")
            else:
                lines.append(item.name)
            if len(lines) >= max_lines:
                lines.append("... (truncated)")
                break
    except PermissionError:
        return None
    return "\n".join(lines) if lines else None


def _find_key_files(root: Path, max_files: int = 5) -> list[tuple[str, str]]:
    """Find and read key entry-point files for LLM context."""
    candidates = [
        "src/__init__.py",
        "src/main.py",
        "src/app.py",
        "src/**/__init__.py",
        "src/**/main.py",
        "main.py",
        "app.py",
        "cli.py",
        "manage.py",
        "index.ts",
        "index.js",
        "app.ts",
        "app.js",
    ]
    results = []
    seen = set()
    for pattern in candidates:
        for path in sorted(root.glob(pattern))[:2]:
            rel = str(path.relative_to(root))
            if rel in seen:
                continue
            seen.add(rel)
            try:
                content = path.read_text(encoding="utf-8", errors="replace")
                results.append((rel, content))
                if len(results) >= max_files:
                    return results
            except (OSError, UnicodeDecodeError):
                pass
    return results
