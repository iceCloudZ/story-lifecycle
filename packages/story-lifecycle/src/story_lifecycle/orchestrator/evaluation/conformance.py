"""conformance 质检器（迭代 1 F2）— 需求↔代码吻合度检查。

judge 逻辑移植自 packages/eval/src/eval/judges.py 的 ConformanceScore（经三轮人工校准的
prompt），**移植进核心包而非 import eval**——依赖方向必须是 eval → story_lifecycle。

接口:
    check_conformance(story_key, workspace, spec_path, diff_text, ...) -> ConformanceResult
    inject_conformance_findings(decision, result) -> list[dict]  # alignment≤2 → HIGH finding

diff 来源（4.3）:
- 生产: git -C <worktree> diff <base>...HEAD（base 从 branches_json 取）
- 回放: done_data 注入 delivery_diff_path，优先读它
- diff >30k token 截断
"""

from __future__ import annotations

import logging
import re
import subprocess
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from ...infra.llm_client import get_llm

log = logging.getLogger("story-lifecycle.conformance")

MAX_DIFF_CHARS = 120_000  # ≈ 30k token
MAX_FILES = 30


class ConformanceResult(BaseModel):
    """需求↔代码吻合度判定结果。"""

    alignment: int = Field(default=0, ge=0, le=5)
    coverage: int = Field(default=0, ge=0, le=5)
    scope_drift: int = Field(default=0, ge=0, le=5)
    findings: list[str] = Field(default_factory=list)
    reference_type: str = ""
    skipped: bool = False
    skip_reason: str = ""
    summary: str = ""


class _ConformanceScore(BaseModel):
    """移植自 eval judges.ConformanceScore 的 schema（prompt 原样保留）。"""

    alignment: int = Field(ge=1, le=5, description="实现与参照物语义一致度")
    coverage: int = Field(ge=1, le=5, description="参照物要求的实现完整度")
    scope_drift: int = Field(ge=1, le=5, description="范围漂移控制(5=无越界改动,1=大量无关改动)")
    reference_type: str = Field(default="", description="实际使用的参照物: spec/prd/tapd")
    findings: list[str] = Field(default_factory=list, description="扣分/缺点的具体问题列表")
    summary: str = Field(default="", description="一句话总评")


def _truncate(text: str, limit: int = MAX_DIFF_CHARS) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n\n... [截断,原文 {len(text)} 字符]"


def _repo_diff(workspace: str, base: str | None = None) -> str:
    """生产路径：git -C <workspace> diff <base>...HEAD。"""
    try:
        cmd = ["git", "--no-pager", "diff"]
        if base:
            cmd += [f"{base}...HEAD"]
        else:
            cmd += ["HEAD"]
        r = subprocess.run(
            cmd, cwd=workspace, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=120,
        )
        if r.returncode == 0:
            return r.stdout
    except Exception as exc:  # noqa: BLE001
        log.warning("[conformance] git diff failed: %s", exc)
    return ""


def _diff_from_paths(workspace: str, files_changed: list[str]) -> str:
    """非 git 产物：按 files_changed 读文件内容拼接。"""
    parts = []
    for f in (files_changed or [])[:MAX_FILES]:
        p = Path(workspace) / f
        if p.exists() and p.is_file():
            try:
                parts.append(f"===== {f} =====\n{p.read_text(encoding='utf-8', errors='replace')[:200_000]}")
            except OSError:
                continue
    return "\n\n".join(parts)


def _collect_diff(
    workspace: str,
    spec_path: str,
    delivery_diff_path: str | None = None,
    files_changed: list[str] | None = None,
    git_base: str | None = None,
) -> str:
    """diff 来源（4.3）：回放注入 > git diff > 文件拼接。"""
    if delivery_diff_path:
        p = Path(delivery_diff_path)
        if p.exists():
            return p.read_text(encoding="utf-8", errors="replace")[:MAX_DIFF_CHARS]
        log.warning("[conformance] delivery_diff_path 不存在: %s", delivery_diff_path)
    diff = _repo_diff(workspace, git_base)
    if diff:
        return diff[:MAX_DIFF_CHARS]
    return _diff_from_paths(workspace, files_changed)


def _resolve_reference(workspace: str, spec_path: str) -> tuple[str, str]:
    """参照物：spec.md > PRD.md；再无则 (空, "")。"""
    for cand in (spec_path, str(Path(workspace) / "story" / "PRD.md")):
        if not cand:
            continue
        p = Path(cand)
        if p.exists() and p.stat().st_size > 0:
            text = p.read_text(encoding="utf-8", errors="replace")
            return text[:120_000], "spec" if "spec" in p.name.lower() else "prd"
    return "", ""


def check_conformance(
    *,
    story_key: str,
    workspace: str,
    spec_path: str = "",
    diff_text: str | None = None,
    delivery_diff_path: str | None = None,
    files_changed: list[str] | None = None,
    git_base: str | None = None,
    timeout: int = 180,
) -> ConformanceResult:
    """检查需求↔代码吻合度（F2）。

    - 参照物缺失 → skipped（记 skip_reason，不产生 finding——§4.2）
    - LLM 失败 → 走 P4 瞬态重试（llm_client 收口），重试耗尽抛异常，由调用方
      转 escalate（fail-closed，不允许跳过检查静默放行）
    """
    ref, ref_type = _resolve_reference(workspace, spec_path)
    if not ref:
        return ConformanceResult(skipped=True, skip_reason="参照物缺失（无 spec/PRD）")

    if diff_text is None:
        diff_text = _collect_diff(
            workspace, spec_path,
            delivery_diff_path=delivery_diff_path,
            files_changed=files_changed,
            git_base=git_base,
        )
    if not diff_text:
        return ConformanceResult(skipped=True, skip_reason="diff 为空")

    type_label = {
        "spec": "设计文档 spec",
        "prd": "需求 PRD",
        "tapd": "TAPD 需求描述",
        "story_refs": "TAPD 需求参照物(链接抓取的正文)",
    }.get(ref_type, ref_type)
    prompt = f"""请判断一次代码交付（merge diff）与**{type_label}**的吻合程度。评分 1-5 分。

# 需求参照物（{type_label}）
```markdown
{_truncate(ref)}
```

# 交付实现（git merge diff 全文/摘要）
```diff
{_truncate(diff_text)}
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
"reference_type": "{ref_type}", "findings": [str], "summary": str}}"""

    llm = get_llm()
    try:
        res = llm.invoke_structured(prompt, _ConformanceScore, temperature=0.1, timeout=timeout)
        return ConformanceResult(
            alignment=res.alignment,
            coverage=res.coverage,
            scope_drift=res.scope_drift,
            findings=res.findings or [],
            reference_type=res.reference_type or ref_type,
            summary=res.summary or "",
        )
    except Exception as exc:
        # fail-closed：LLM 失败不允许跳过检查静默放行——抛给调用方转 escalate
        log.warning("[%s] conformance LLM 失败: %s", story_key, exc)
        raise


def inject_conformance_findings(
    result: ConformanceResult,
    alignment_threshold: int = 2,
) -> list[dict]:
    """conformance 结果转 findings（4.4，迭代 1 收尾修订）：

    - alignment ≤ threshold **或 coverage ≤ 2** → HIGH finding（管线内完整交付要求）
    - alignment == threshold+1 且 coverage > 2 → MEDIUM finding（记录不阻断）
    - alignment ≥ threshold+2 且 coverage ≥ 3 → 无 finding（分数已写入 done_data）
    """
    if result.skipped:
        return []
    findings: list[dict] = []
    desc = (
        f"conformance: alignment={result.alignment} coverage={result.coverage} "
        f"scope_drift={result.scope_drift} (ref={result.reference_type}) | {result.summary}"
    )
    if result.alignment <= alignment_threshold or result.coverage <= 2:
        findings.append({
            "severity": "HIGH",
            "category": "conformance",
            "description": desc,
            "location": "conformance_check",
        })
    elif result.alignment == alignment_threshold + 1 and result.coverage > 2:
        findings.append({
            "severity": "MEDIUM",
            "category": "conformance",
            "description": desc,
            "location": "conformance_check",
        })
    return findings
