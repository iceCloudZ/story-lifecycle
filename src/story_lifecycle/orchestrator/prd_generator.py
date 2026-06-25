"""Self-contained PRD generator for the story intake phase.

This module owns the prompt contract for preparing ``PRD.md`` before a story
enters Design/Build/Verify. It deliberately does not depend on project-local
agent skills from other workspaces.
"""

from __future__ import annotations

import base64
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

import httpx

from pydantic import BaseModel, Field

from ..llm_client import LLMClient, get_llm, get_vision_llm

log = logging.getLogger("story-lifecycle.prd-generator")


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
    image_urls: list[str] = field(default_factory=list)
    local_image_paths: list[str] = field(default_factory=list)


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
    images = _prepare_images(
        source.description, source.image_urls, source.local_image_paths
    )

    if images and _use_kimi_cli_vision():
        log.info(
            "Using Kimi CLI vision for %s with %d image(s)",
            source.story_key,
            len(images),
        )
        content = _invoke_kimi_cli_vision(prompt, images)
        data = _parse_json_or_none(content)
        if data is None:
            raise ValueError(
                f"Cannot parse Kimi CLI vision response as JSON. "
                f"First 500 chars: {content[:500]!r}"
            )
        return PrdGenerationResult.model_construct(
            **{k: v for k, v in data.items() if k in PrdGenerationResult.model_fields}
        )

    vision_llm = get_vision_llm() if images else None
    if vision_llm is not None:
        log.info(
            "Using vision LLM (%s) for %s with %d image(s)",
            vision_llm.model,
            source.story_key,
            len(images),
        )
        content = vision_llm.invoke_vision(
            prompt,
            images,
            temperature=0.1,
            timeout=180,
            max_tokens=4000,
        )
        data = _parse_json_or_none(content)
        if data is None:
            raise ValueError(
                f"Cannot parse vision LLM response as JSON. "
                f"First 500 chars: {content[:500]!r}"
            )
        return PrdGenerationResult.model_construct(
            **{k: v for k, v in data.items() if k in PrdGenerationResult.model_fields}
        )

    if images:
        log.warning(
            "Source %s contains %d image(s) but no vision LLM is configured; "
            "falling back to text-only PRD generation.",
            source.story_key,
            len(images),
        )

    result = get_llm().invoke_structured(
        prompt,
        PrdGenerationResult,
        temperature=0.1,
        timeout=120,
        max_tokens=3000,
    )
    return result


# ── helpers ──


def _parse_json_or_none(content: str) -> dict | None:
    """Reuse the LLM client's robust JSON parser."""
    return LLMClient._parse_json(content)


def _use_kimi_cli_vision() -> bool:
    """Check whether the configured vision model should use the local Kimi CLI.

    Kimi's `kimi-for-coding` model is restricted to Coding Agents and cannot be
    called through the OpenAI-compatible HTTP API, so we shell out to the CLI.
    """
    import os

    provider = os.environ.get("STORY_VISION_PROVIDER", "")
    if provider.lower() == "kimi-cli":
        return True
    model = os.environ.get("STORY_VISION_MODEL", "")
    return model.lower() in {"kimi-for-coding", "kimi-k2.5", "kimi-k2"}


def _invoke_kimi_cli_vision(prompt: str, images: list[str]) -> str:
    """Run a headless Kimi CLI prompt with images and return its text answer."""
    from ..llm_client_kimi_cli import KimiCliClient

    # Prefer the configured vision model if present, otherwise default.
    import os

    model = os.environ.get("STORY_VISION_MODEL", "kimi-for-coding")
    client = KimiCliClient(model=model)
    return client.invoke_vision(
        prompt,
        images,
        timeout=240,
        max_tokens=4000,
    )


def _extract_image_urls(html_or_md: str) -> list[str]:
    """Extract image URLs/paths from raw HTML or markdown text."""
    if not html_or_md:
        return []
    urls: list[str] = []
    # Markdown images: ![alt](url)
    urls.extend(re.findall(r"!\[.*?\]\(([^\s)]+)\)", html_or_md))
    # HTML images: <img src="url" ...> or <img src='url' ...>
    urls.extend(re.findall(r"<img[^>]+src=[\"']([^\"']+)[\"']", html_or_md, re.IGNORECASE))
    # Dedupe while preserving order
    seen = set()
    result = []
    for url in urls:
        if url in seen:
            continue
        seen.add(url)
        result.append(url)
    return result


def _prepare_images(
    description: str,
    image_urls: list[str],
    local_image_paths: list[str],
) -> list[str]:
    """Return a list of image URLs/data-URLs/paths ready for a vision LLM.

    Priority:
    1. User-uploaded local image paths (most reliable).
    2. Public URLs downloaded and inlined as base64 data URLs.
    3. TAPD / other authenticated URLs are kept as references; the prompt
       tells the model they may require login and the user can upload them
       if needed.
    """
    # User-uploaded images always take precedence.
    if local_image_paths:
        return [p for p in local_image_paths if Path(p).exists()]

    urls = list(image_urls) or _extract_image_urls(description)
    if not urls:
        return []

    images: list[str] = []
    for url in urls:
        if url.startswith("data:"):
            images.append(url)
            continue

        # Local file path (Windows or Unix): pass through for Kimi CLI.
        if Path(url).exists():
            images.append(url)
            continue

        # Public URLs: try to download and inline as base64.
        parsed = urlparse(url)
        if parsed.scheme in ("http", "https") and "tapd.cn" not in parsed.netloc:
            try:
                data_url = _download_as_data_url(url)
                if data_url:
                    images.append(data_url)
                    continue
            except Exception as exc:
                log.warning("Failed to inline image %s: %s", url, exc)

        # TAPD / authenticated URLs: pass the URL through so the model can
        # cite it; actual understanding requires the user to upload the image.
        images.append(url)

    return images


def _download_as_data_url(url: str, timeout: float = 15.0) -> str | None:
    """Download a public image and return it as a base64 data URL."""
    resp = httpx.get(url, timeout=timeout, follow_redirects=True)
    resp.raise_for_status()
    content_type = resp.headers.get("content-type", "image/png").split(";")[0]
    if not content_type.startswith("image/"):
        content_type = "image/png"
    encoded = base64.b64encode(resp.content).decode("ascii")
    return f"data:{content_type};base64,{encoded}"


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

## 图片说明

来源正文中可能引用了图片链接（如 TAPD 截图）。这些链接如果附带为图片消息，请直接阅读图片内容；如果仅以 URL 形式出现且无法访问，请将其作为参考引用，不要臆测图片内容。
- 请将可访问的图片视为需求来源的一部分，图片中可能包含流程图、原型图、表格、接口字段或截图。
- 请结合正文与可访问图片生成 PRD。
- 如果图片无法访问或关键信息无法确认，请在 PRD 中简要引用图片链接，并把不确定的细节放入“待确认问题”。
- 如果未附带图片且正文中无图片链接，则忽略本节。

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
