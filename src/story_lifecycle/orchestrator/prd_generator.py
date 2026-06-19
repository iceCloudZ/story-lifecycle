"""Self-contained PRD generator for the story intake phase.

This module owns the prompt contract for preparing ``PRD.md`` before a story
enters Design/Build/Verify. It deliberately does not depend on project-local
agent skills from other workspaces.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, Field

from ..llm_client import get_llm


@dataclass(frozen=True)
class StorySourceSnapshot:
    story_key: str
    source_type: str
    source_id: str
    title: str
    description: str = ""
    url: str = ""
    priority: str = ""
    owner: str = ""
    status: str = ""


class PrdGenerationResult(BaseModel):
    action: Literal[
        "generated",
        "manual_download_required",
        "needs_clarification",
        "failed",
    ]
    dingtalk_links: list[str] = Field(default_factory=list)
    markdown: str = ""
    summary: str = ""
    questions: list[str] = Field(default_factory=list)


def generate_prd_from_source(source: StorySourceSnapshot) -> PrdGenerationResult:
    """Ask the built-in PRD generator LLM to prepare or route PRD intake."""
    prompt = build_prd_generator_prompt(source)
    result = get_llm().invoke_structured(
        prompt,
        PrdGenerationResult,
        temperature=0.1,
        timeout=120,
        max_tokens=3000,
    )
    return result


def build_prd_generator_prompt(source: StorySourceSnapshot) -> str:
    """Build a self-contained prd-generator prompt.

    Keep this prompt source-agnostic: current callers use TAPD, but future
    callers can pass GitHub Issue, manual input, or other source snapshots.
    """
    return f"""你是 story-lifecycle 内置的 prd-generator。

不要依赖外部 hc-all skill，不要假设本机存在任何其他仓库的 skill 文档。
你的任务是在 Intake 阶段准备进入 Design 前必须存在的 `PRD.md`。

## 来源信息

- story_key: {source.story_key}
- source_type: {source.source_type}
- source_id: {source.source_id}
- title: {source.title}
- url: {source.url}
- priority: {source.priority}
- owner: {source.owner}
- status: {source.status}

## 来源正文

{source.description[:12000]}

## 决策规则

1. 如果来源正文里只有钉钉/阿里文档/语雀等外部文档链接，或核心需求明显在外部链接中：
   - action 返回 `"manual_download_required"`
   - dingtalk_links 返回所有需要人工打开/下载/复制的链接
   - markdown 留空
   - summary 用一句话说明为什么需要人工下载
2. 如果来源正文已经包含足够需求内容：
   - action 返回 `"generated"`
   - markdown 返回完整 PRD 正文
3. 如果缺少关键业务决策，无法生成可用 PRD：
   - action 返回 `"needs_clarification"`
   - questions 返回需要人工确认的问题
4. 如果遇到无法处理的输入：
   - action 返回 `"failed"`
   - summary 说明原因

## PRD 正文要求

生成的 markdown 是给后续 Design 阶段使用的轻量 Intake PRD，不是完整产品交付文档。
默认控制在 800-1500 个中文字符；如果来源信息很少，可以更短。

必须遵守：

- 只整理来源中明确出现的信息，不要补充来源中没有的接口名、字段名、默认值、性能指标、兼容策略或实现方案。
- 如果你根据常识推断了内容，必须标注为“推断”或放入“待确认问题”。
- 不要展开技术方案、测试方案、运营培训、安全测试清单；这些留给后续 Design/Build/Verify 阶段。
- 句子要短，列表要少，优先保留业务规则、范围和验收口径。

推荐结构：

1. 标题
2. 需求摘要：3-5 句话说明要做什么、为什么做。
3. 业务规则：只写来源中明确的规则；不确定的规则写“待确认”。
4. 范围：包含/不包含。
5. 验收标准：3-6 条，贴近业务结果，不写实现步骤。
6. 待确认问题：没有则写“暂无”。
7. 风险提示：仅当来源明确涉及前端可控参数、权限、金额、状态流转、用户数据、营销人群等风险时写 1-3 条；否则写“暂无明显风险”。

## 输出格式

只返回 JSON，不要 markdown fence，不要解释文字：

{{
  "action": "generated|manual_download_required|needs_clarification|failed",
  "dingtalk_links": ["https://..."],
  "markdown": "# PRD 标题\\n...",
  "summary": "一句话摘要",
  "questions": ["需要人工确认的问题"]
}}
"""
