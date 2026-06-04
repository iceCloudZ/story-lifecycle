"""Idea expander — Step 0a: idea → requirements.md via LLM dialog."""

from __future__ import annotations

import logging
from pathlib import Path

from .llm import call_llm
from .state import update_step

log = logging.getLogger(__name__)

SYSTEM_PROMPT = """你是一位资深产品经理。用户有一个项目 idea，你需要通过对话帮助他澄清需求，最终生成一份结构化的需求文档。

要求：
1. 先问 3-5 个关键问题（目标用户、核心功能、技术偏好、约束条件）
2. 每次只问 1-2 个问题，等用户回答后再继续
3. 收集够信息后，生成 requirements.md 格式的需求文档
4. 需求文档包含：项目概述、目标用户、核心功能列表、非功能性需求、技术约束"""

REQUIREMENTS_TEMPLATE = """# {title}

## 项目概述

{overview}

## 目标用户

{target_users}

## 核心功能

{features}

## 非功能性需求

{non_functional}

## 技术约束

{tech_constraints}

## 验收标准

{acceptance_criteria}
"""


def expand_idea_to_requirements(
    idea: str,
    *,
    cwd: str | None = None,
    max_rounds: int = 5,
) -> str:
    """Expand an idea into requirements via LLM. Returns the requirements markdown.

    This is the single-shot version that takes the full idea description and
    generates requirements in one pass. For interactive mode, use
    start_idea_dialog() instead.
    """
    prompt = f"""用户的 idea：{idea}

请基于这个 idea，生成一份完整的需求文档（中文）。包含：
1. 项目概述（一段话描述项目目标和价值）
2. 目标用户（主要用户群体和使用场景）
3. 核心功能列表（每个功能一行，用 - 开头，按优先级排序）
4. 非功能性需求（性能、安全、可用性等）
5. 技术约束（如果有明显的技术选型要求）

直接输出 Markdown 格式的需求文档，不要包含额外的解释。"""

    content = call_llm(prompt, system=SYSTEM_PROMPT, temperature=0.3)
    _save_requirements(content, cwd=cwd)
    update_step("step_0a", {"requirements_generated": True}, cwd=cwd)
    return content


def start_idea_dialog() -> list[dict]:
    """Get the first round of clarifying questions for the user's idea.

    Returns a list of question dicts: [{"question": "...", "field": "..."}]
    """
    prompt = """用户要开始一个新项目。请提出 3 个最关键的澄清问题，帮助理解项目需求。

每个问题格式：
{"question": "问题内容", "field": "字段名"}

返回 JSON 数组。"""

    from .llm import call_llm_json

    result = call_llm_json(prompt, system=SYSTEM_PROMPT, temperature=0.3)
    if isinstance(result, list):
        return result
    return [{"question": "请描述你的项目 idea", "field": "idea"}]


def _save_requirements(content: str, *, cwd: str | None = None) -> Path:
    """Save requirements to .story/planning/requirements.md."""
    root = Path(cwd) if cwd else Path.cwd()
    planning_dir = root / ".story" / "planning"
    planning_dir.mkdir(parents=True, exist_ok=True)
    path = planning_dir / "requirements.md"
    path.write_text(content, encoding="utf-8")
    return path
