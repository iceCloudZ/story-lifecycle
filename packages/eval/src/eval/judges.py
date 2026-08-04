"""LLM judges — 离线质量评分（Pydantic schema + Go 端点调用）。

复用 ``story_lifecycle.infra.llm_client.LLMClient``；所有调用**串行**、
``temperature=0``、429 指数退避（初始 5s ×2 上限 300s）、失败重试 2 次后
记录 error 不中断全量任务。judge prompt 中文、reference-based（gold artifact
全文注入作参照）。

模型选择: ``EVAL_LLM_BASE_URL/API_KEY/MODEL`` 覆盖（默认取 Go 端点 +
``OPENCODE_API_KEY``）;见 ``configure_llm_env()``。
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any

from pydantic import BaseModel, Field

log = logging.getLogger("eval.judges")

DEFAULT_BASE_URL = "https://opencode.ai/zen/go/v1"
DEFAULT_MODEL = "deepseek-v4-flash"

BACKOFF_INITIAL = 5.0
BACKOFF_MULT = 2.0
BACKOFF_MAX = 300.0
MAX_HTTP_RETRIES = 2


def configure_llm_env() -> None:
    """把 STORY_LLM_* 指向 Go 端点（judge 与回放管线共用）。

    EVAL_LLM_* 优先（对比 judge 模型用）;否则 EVAL 未设 → 用 Go 默认。
    OPENCODE_API_KEY 缺失时仅打 warning（调用时才会真正失败）。
    """
    base = os.environ.get("EVAL_LLM_BASE_URL") or DEFAULT_BASE_URL
    model = os.environ.get("EVAL_LLM_MODEL") or DEFAULT_MODEL
    key = os.environ.get("EVAL_LLM_API_KEY") or os.environ.get("OPENCODE_API_KEY")
    if not key:
        log.warning("OPENCODE_API_KEY 未设置——judge 调用将失败（设置环境变量后重试）")
    os.environ.setdefault("STORY_LLM_BASE_URL", base)
    os.environ.setdefault("STORY_LLM_MODEL", model)
    if key:
        os.environ.setdefault("STORY_LLM_API_KEY", key)


class BaseScore(BaseModel):
    """通用评分结构:各维度 1-5 分 + findings + summary。"""

    findings: list[str] = Field(default_factory=list, description="扣分/缺点的具体问题列表")
    summary: str = Field(default="", description="一句话总评")


class SpecScore(BaseScore):
    """spec 质量:对照 PRD 需求覆盖 / 对照模板必填章节 / 验收标准可测。"""

    completeness: int = Field(ge=1, le=5, description="需求覆盖度:对照 PRD 是否覆盖全部需求点")
    template_compliance: int = Field(ge=1, le=5, description="模板符合度:对照 spec-template 必填 Release 章节")
    acceptability: int = Field(ge=1, le=5, description="验收标准:明确、可测、无歧义")


class PlanScore(BaseScore):
    """plan 质量:步骤具体可执行 / 与 spec 对齐 / 每步可检验。"""

    specificity: int = Field(ge=1, le=5, description="步骤具体性:每一步具体可执行")
    spec_alignment: int = Field(ge=1, le=5, description="与 spec 的对齐度")
    verifiability: int = Field(ge=1, le=5, description="每步可检验性")


class ConformanceScore(BaseScore):
    """回放产出与 spec 的吻合度:alignment（实现与 spec 语义一致）+ coverage（覆盖度）。"""

    alignment: int = Field(ge=1, le=5, description="实现与 spec 语义一致度")
    coverage: int = Field(ge=1, le=5, description="spec 要求的实现完整度")


class _LLM:
    """惰性单例 + 429 退避的 LLMClient 包装。"""

    _client = None

    @classmethod
    def client(cls):
        if cls._client is None:
            from story_lifecycle.infra.llm_client import LLMClient

            cls._client = LLMClient()
        return cls._client

    @classmethod
    def invoke_structured(cls, prompt: str, schema: type[BaseModel]) -> BaseModel:
        """带 429 指数退避 + 2 次重试的 invoke_structured。"""
        last_exc: Exception | None = None
        for attempt in range(MAX_HTTP_RETRIES + 1):
            try:
                return cls.client().invoke_structured(
                    prompt,
                    schema,
                    system="你是严谨的软件工程质量评审专家。只输出合法 JSON。",
                    temperature=0,
                    timeout=180,
                    max_tokens=4096,
                )
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                if not _is_retryable(exc):
                    raise
                delay = min(BACKOFF_INITIAL * (BACKOFF_MULT**attempt), BACKOFF_MAX)
                log.warning(
                    "LLM 调用失败(attempt %d/%d): %s; %.0fs 后退避重试",
                    attempt + 1,
                    MAX_HTTP_RETRIES + 1,
                    exc,
                    delay,
                )
                time.sleep(delay)
        raise RuntimeError(f"LLM 调用重试耗尽: {last_exc}")


def _is_retryable(exc: Exception) -> bool:
    """429/5xx/超时 → 可退避重试。"""
    text = f"{exc.__class__.__name__}: {exc}"
    if "429" in text or "too many" in text.lower():
        return True
    if "50" in text[:4] and text[2:3].isdigit():
        return True
    if isinstance(exc, TimeoutError) or "timed out" in text.lower() or "timeout" in text.lower():
        return True
    return False


def _truncate(text: str, limit: int = 120_000) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n\n... [截断,原文 {len(text)} 字符]"


def _head(text: str, limit: int = 30_000) -> str:
    return _truncate(text, limit)


def judge_spec(prd_text: str, spec_text: str, template_text: str = "") -> SpecScore:
    """SpecScore:参考 PRD + spec-template 评 spec。"""
    prompt = f"""请对以下软件需求「设计文档(spec)」质量打分。评分维度各 1-5 分（5=优秀,1=很差）。

# 评审对象:spec.md
```markdown
{spec_text}
```

# 参照 1:原始需求 PRD（gold,人工验收过的需求源）
```markdown
{_truncate(prd_text, 200_000)}
```

# 参照 2:spec 模板要求（必填 Release 章节:SQL 变更 / Nacos 配置变更 / 验收测试 /
验收计划 / 大表名单;无变更须写「无变更」;否则不可省略）
```markdown
{template_text or "(未提供模板,按软件工程常识评审 template_compliance)"}
```

评分要求:
- completeness:spec 是否覆盖 PRD 的全部需求点（逐条对照,漏了哪些写明）
- template_compliance:必填章节是否齐全且内容可执行
- acceptability:验收标准是否明确、可测（有具体断言/指标,不是「功能正常」这类空话）
findings 列 2-6 条具体问题（没有则留空数组）;summary 一句话总评。

只输出 JSON 对象:{{"completeness": int, "template_compliance": int,
"acceptability": int, "findings": [str], "summary": str}}"""
    return _LLM.invoke_structured(prompt, SpecScore)  # type: ignore[return-value]


def judge_plan(plan_text: str, spec_text: str) -> PlanScore:
    """PlanScore:参考 spec 评 plan。"""
    prompt = f"""请对以下软件需求「实施计划(plan)」质量打分。评分维度各 1-5 分（5=优秀,1=很差）。

# 评审对象:plan.md
```markdown
{_truncate(plan_text, 200_000)}
```

# 参照:设计文档 spec（gold,人工验收过）
```markdown
{_truncate(spec_text, 200_000)}
```

评分要求:
- specificity:每一步是否具体可执行（有文件路径/命令/明确动作,不是「实现相关功能」这类空话）
- spec_alignment:计划是否与 spec 的设计、范围、验收口径一致
- verifiability:每一步是否有可检验的结果（测试/构建/人工核对）
findings 列 2-6 条具体问题;summary 一句话总评。

只输出 JSON 对象:{{"specificity": int, "spec_alignment": int,
"verifiability": int, "findings": [str], "summary": str}}"""
    return _LLM.invoke_structured(prompt, PlanScore)  # type: ignore[return-value]


def judge_conformance(spec_text: str, build_artifacts: dict[str, str]) -> ConformanceScore:
    """ConformanceScore:回放 build 产出（git diff / 落地 artifact 文本）与 spec 吻合度。"""
    artifact_block = "\n\n".join(
        f"### {name}\n```\n{_truncate(text, 60_000)}\n```" for name, text in build_artifacts.items()
    )
    if not artifact_block:
        artifact_block = "（无任何落地产出文本）"
    prompt = f"""请判断一次 AI 编码回放产出的**实现**与**设计文档(spec)**的吻合程度。评分 1-5 分。

# 设计文档 spec（gold）
```markdown
{_truncate(spec_text, 150_000)}
```

# 回放 build 产出（git diff 摘要 / 新增文件 / 测试报告）
{artifact_block}

评分要求:
- alignment:实现是否与 spec 的语义一致（数据结构/接口/流程,不要求逐字相同）
- coverage:spec 要求的实现点被覆盖的比例（缺了哪些写明）
findings 列 2-6 条具体差异;summary 一句话总评。

只输出 JSON 对象:{{"alignment": int, "coverage": int,
"findings": [str], "summary": str}}"""
    return _LLM.invoke_structured(prompt, ConformanceScore)  # type: ignore[return-value]
