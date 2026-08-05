"""LLM judges — 离线质量评分（Pydantic schema + OpenAI-compatible 端点调用）。

复用 ``story_lifecycle.infra.llm_client.LLMClient``；``temperature=0``、
429 指数退避（初始 5s ×2 上限 300s）、失败重试 2 次后记录 error 不中断全量任务。
judge prompt 中文、reference-based（gold artifact 全文注入作参照）。

模型选择: ``EVAL_LLM_BASE_URL/API_KEY/MODEL`` 覆盖（默认取 Go 端点 +
``OPENCODE_API_KEY``）;见 ``configure_llm_env()``。

并发说明: judge 模块本身保持单调用语义；scanall 等外层调用方可通过
``EVAL_LLM_CONCURRENCY`` 并发。token 消耗由本模块挂载的 hook 统计，
不依赖 story.db 的 trace 表。
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

log = logging.getLogger("eval.judges")

DEFAULT_BASE_URL = "https://opencode.ai/zen/go/v1"
DEFAULT_MODEL = "deepseek-v4-flash"

BACKOFF_INITIAL = 5.0
BACKOFF_MULT = 2.0
BACKOFF_MAX = 300.0
MAX_HTTP_RETRIES = 2

# 线程安全 token 统计
_token_lock = threading.Lock()
_token_usage: dict[str, int] = {"prompt": 0, "completion": 0, "total": 0, "calls": 0}
_token_hook_installed = False


def _install_token_hook() -> None:
    """挂载 LLMClient._trace 钩子，把 usage 累加到本模块计数器。

    不破坏原有 trace 行为（仍会写 story.db），只额外做一份 eval 专用统计，
    避免高并发下 story.db 锁竞争导致丢数。
    """
    global _token_hook_installed
    if _token_hook_installed:
        return
    try:
        from story_lifecycle.infra.llm_client import LLMClient

        original_trace = LLMClient._trace

        def _patched_trace(usage: dict, duration_ms: int, **kwargs):
            with _token_lock:
                _token_usage["prompt"] += usage.get("prompt_tokens", 0)
                _token_usage["completion"] += usage.get("completion_tokens", 0)
                _token_usage["total"] += usage.get("total_tokens", 0)
                _token_usage["calls"] += 1
            return original_trace(usage, duration_ms, **kwargs)

        LLMClient._trace = staticmethod(_patched_trace)  # type: ignore[assignment]
        _token_hook_installed = True
    except Exception as e:
        log.warning("token 统计钩子挂载失败: %s", e)


def get_token_usage() -> dict[str, int]:
    """返回当前进程累计 token 消耗（prompt/completion/total/calls）。"""
    with _token_lock:
        return dict(_token_usage)


def reset_token_usage() -> None:
    """重置计数器，通常在 scanall 启动前调用。"""
    global _token_usage
    with _token_lock:
        _token_usage = {"prompt": 0, "completion": 0, "total": 0, "calls": 0}


def _load_dotenv(path: Path, override: bool = False) -> None:
    """手工解析简单的 KEY=VALUE .env 文件（无外部依赖）。"""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"\'')
        if not key or not value:
            continue
        if override or key not in os.environ:
            os.environ[key] = value


def configure_llm_env(env_file: str | Path | None = None) -> None:
    """把 STORY_LLM_* 指向 Go 端点（judge 与回放管线共用）。

    EVAL_LLM_* 优先（对比 judge 模型用）;否则 EVAL 未设 → 用 Go 默认。
    若 ``env_file`` 存在，自动加载（并覆盖已有 EVAL_LLM_*/STORY_LLM_*，
    用于临时切 DeepSeek 官方端点）;否则尝试 ``packages/eval/dataset/.env``。
    OPENCODE_API_KEY 缺失时仅打 warning（调用时才会真正失败）。
    """
    _install_token_hook()
    if env_file is not None:
        _load_dotenv(Path(env_file), override=True)
        # 显式 env_file 时强制覆盖 STORY_LLM_*（CLI 启动可能已按 Go 端点 setdefault）
        for k in ("STORY_LLM_BASE_URL", "STORY_LLM_MODEL", "STORY_LLM_API_KEY"):
            os.environ.pop(k, None)
        # 客户端单例可能已按旧端点创建，重置以便重建
        _LLM._client = None
    else:
        _load_dotenv(Path(__file__).resolve().parent.parent.parent / "dataset" / ".env")
    base = os.environ.get("EVAL_LLM_BASE_URL") or DEFAULT_BASE_URL
    model = os.environ.get("EVAL_LLM_MODEL") or DEFAULT_MODEL
    key = os.environ.get("EVAL_LLM_API_KEY") or os.environ.get("OPENCODE_API_KEY")
    if not key:
        log.warning("OPENCODE_API_KEY 未设置——judge 调用将失败（设置环境变量后重试）")
    os.environ["STORY_LLM_BASE_URL"] = base
    os.environ["STORY_LLM_MODEL"] = model
    if key:
        os.environ["STORY_LLM_API_KEY"] = key


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
    """实现（merge diff）与需求参照物的吻合度。

    参照物优先级: C 源 spec > C 源 PRD > B 源 TAPD 需求描述。
    评分须标注实际用的参照物类型。
    """

    alignment: int = Field(ge=1, le=5, description="实现与参照物语义一致度")
    coverage: int = Field(ge=1, le=5, description="参照物要求的实现完整度")
    scope_drift: int = Field(ge=1, le=5, description="范围漂移控制(5=无越界改动,1=大量无关改动)")
    reference_type: str = Field(default="", description="实际使用的参照物: spec/prd/tapd")


class DeliveryScore(BaseScore):
    """交付质量:commit message 质量 / 提交粒度 / 返工迹象。"""

    message_quality: int = Field(ge=1, le=5, description="commit message 质量(描述清晰、关联需求)")
    granularity: int = Field(ge=1, le=5, description="提交粒度(逻辑独立、大小适中)")
    rework: int = Field(ge=1, le=5, description="返工控制(5=无 revert/fixup/反复修,1=大量返工)")


class MergeSummary(BaseModel):
    """无关联 merge 的语义摘要（兜底评分 + 第二轮模糊关联素材）。"""

    summary: str = Field(description="该 merge 实际做了什么的一段语义摘要(100-300字)")
    topics: list[str] = Field(default_factory=list, description="改动主题标签 3-8 个")


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
        """带 429 指数退避 + parse 修复重试的 invoke_structured。

        - 首次调用即强制「只输出 JSON,不要任何解释/思考过程」。
        - 若仍输出数组,追加「严禁数组」重试。
        - 若输出无法解析为 JSON 对象,追加更严格的「只输出 JSON」重试。
        """
        system = (
            "你是严谨的软件工程质量评审专家。"
            "必须只输出合法 JSON 对象,不要任何解释、markdown、代码块、思考过程或前后缀。"
        )
        example = json.dumps(
            {n: _schema_example(f) for n, f in schema.model_fields.items()},
            ensure_ascii=False,
        )
        last_exc: Exception | None = None
        for attempt in range(MAX_HTTP_RETRIES + 1):
            try:
                return cls.client().invoke_structured(
                    prompt,
                    schema,
                    system=system,
                    temperature=0,
                    timeout=180,
                    max_tokens=4096,
                )
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                if _is_list_output(exc):
                    log.warning("LLM 输出数组而非对象,补「严禁数组」提示重试")
                    try:
                        return cls.client().invoke_structured(
                            prompt + f"\n\n**严禁输出 JSON 数组/列表**——本任务是对象评分,必须输出形如 {example} 的单个 JSON 对象,不要任何其他内容。",
                            schema,
                            system=system + " 严禁输出数组,只能输出单个 JSON 对象。",
                            temperature=0,
                            timeout=180,
                            max_tokens=4096,
                        )
                    except Exception as e2:  # noqa: BLE001
                        raise RuntimeError(f"数组输出修复重试也失败: {e2}") from e2
                if _is_parse_failure(exc):
                    log.warning("LLM 输出无法解析为 JSON,补「只输出 JSON」提示重试")
                    try:
                        return cls.client().invoke_structured(
                            prompt + "\n\n**要求**：不要写任何分析、解释、思考过程或自然语言,直接输出且仅输出一个合法 JSON 对象。"
                            f"必须形如 {example} 。第一个字符必须是 {{,最后一个字符必须是 }}。",
                            schema,
                            system=system + " 只能输出 JSON 对象,任何非 JSON 内容都视为错误。",
                            temperature=0,
                            timeout=180,
                            max_tokens=4096,
                        )
                    except Exception as e2:  # noqa: BLE001
                        raise RuntimeError(f"JSON 修复重试也失败: {e2}") from e2
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


def _schema_example(field) -> Any:
    """给评分维度的示例值（int 给 3,str 给空串,list 给空数组）。"""
    from typing import get_origin

    ann = field.annotation
    if ann is int:
        return 3
    if ann is str:
        return ""
    if get_origin(ann) is list:
        return []
    return None


def _is_list_output(exc: Exception) -> bool:
    """LLM 把对象输出成数组的典型 ValidationError。"""
    text = f"{exc.__class__.__name__}: {exc}"
    if "ValidationError" not in text and "validation error" not in text.lower():
        return False
    if "input_value=[], input_type=list" in text or "input_type=list" in text:
        return True
    if "should be a valid dictionary" in text or "model_type" in text:
        return True
    return False


def _is_parse_failure(exc: Exception) -> bool:
    """LLM 输出不是可解析 JSON 对象（含 JSON 解析失败、ValidationError）。"""
    text = f"{exc.__class__.__name__}: {exc}".lower()
    if "cannot parse" in text:
        return True
    if "json" in text and ("decode" in text or "parse" in text):
        return True
    if "validation" in text and "model_type" in text:
        return True
    if "expecting" in text and "json" in text:
        return True
    return False


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


def judge_conformance(reference_text: str, reference_type: str, diff_text: str) -> ConformanceScore:
    """ConformanceScore:实现（merge diff）vs 需求参照物（spec/PRD/TAPD 描述）。

    diff 大按文件分批由调用方负责（本函数处理单批）。
    """
    type_label = {
        "spec": "设计文档 spec",
        "prd": "需求 PRD",
        "tapd": "TAPD 需求描述",
        "story_refs": "TAPD 需求参照物(链接抓取的正文)",
    }.get(reference_type, reference_type)
    prompt = f"""请判断一次代码交付（merge diff）与**{type_label}**的吻合程度。评分 1-5 分。

# 需求参照物（{type_label}）
```markdown
{_truncate(reference_text, 120_000)}
```

# 交付实现（git merge diff 全文/摘要）
```diff
{_truncate(diff_text, 120_000)}
```

评分要求:
- alignment: **已实现部分**与参照物的语义一致度（数据结构/接口/流程,不要求逐字相同）。
  跨服务需求在单仓只交付切片时,若已实现部分与参照物语义一致,alignment 不得给 1,
  低覆盖体现在 coverage;alignment=1 仅用于「diff 中没有任何内容与参照物对应」。
- coverage: 参照物要求的**完整度**——要求点被覆盖的比例（缺了哪些写明;单仓切片
  交付属于覆盖不全,不降低 alignment）
- scope_drift:是否混入与需求无关的改动（5=严格按范围,1=大量无关改动）
findings 列 2-6 条具体差异;summary 一句话总评。

只输出 JSON 对象:{{"alignment": int, "coverage": int, "scope_drift": int,
"reference_type": "{reference_type}", "findings": [str], "summary": str}}"""
    return _LLM.invoke_structured(prompt, ConformanceScore)  # type: ignore[return-value]


def judge_delivery(commits: list[dict], repo: str, branch: str) -> DeliveryScore:
    """DeliveryScore:commit message 质量 / 提交粒度 / 返工迹象。"""
    commit_block = "\n".join(
        f"- [{c.get('date', '')[:10]}] {c.get('subject', '')[:200]}" for c in commits[:80]
    )
    if not commit_block:
        commit_block = "（无提交信息）"
    prompt = f"""请评审一次代码交付的提交质量。评分 1-5 分。

# 交付信息
- repo: {repo}
- branch: {branch}
- 提交数: {len(commits)}

# commit 列表
{commit_block}

评分要求:
- message_quality:commit message 是否清晰描述改动、是否关联需求/问题
- granularity:提交粒度是否逻辑独立、大小适中（不把一堆无关改动揉在一个提交里）
- rework:是否有 revert/fixup/反复修复同一处的返工迹象
findings 列 2-6 条具体问题;summary 一句话总评。

只输出 JSON 对象:{{"message_quality": int, "granularity": int, "rework": int,
"findings": [str], "summary": str}}"""
    return _LLM.invoke_structured(prompt, DeliveryScore)  # type: ignore[return-value]


def judge_merge_summary(commits: list[dict], repo: str, branch: str, diffstat: dict) -> MergeSummary:
    """MergeSummary:该 merge 实际做了什么（语义摘要 + 主题标签）。

    无关联 story 的 merge 兜底;摘要写回索引反哺第二轮模糊关联。
    """
    commit_block = "\n".join(
        f"- [{c.get('date', '')[:10]}] {c.get('subject', '')[:200]}" for c in commits[:80]
    )
    if not commit_block:
        commit_block = "（无提交信息）"
    stat = diffstat or {}
    stat_block = (
        f"- 文件: {stat.get('files', 0)} 个,新增 {stat.get('insertions', 0)} 行,"
        f"删除 {stat.get('deletions', 0)} 行"
    )
    prompt = f"""请归纳一次代码交付（git merge）实际做了什么。输出语义摘要 + 主题标签。

# 交付信息
- repo: {repo}
- branch: {branch}
- 提交数: {len(commits)}
{stat_block}

# commit 列表
{commit_block}

要求:
- summary: 100-300 字,概括该 merge 实现的功能/修复/重构（从 commit 角度,不猜需求）
- topics: 3-8 个主题标签（如: 风控、还款计划、报表、bugfix、配置项、重构）

只输出 JSON 对象:{{"summary": str, "topics": [str]}}"""
    return _LLM.invoke_structured(prompt, MergeSummary)  # type: ignore[return-value]
