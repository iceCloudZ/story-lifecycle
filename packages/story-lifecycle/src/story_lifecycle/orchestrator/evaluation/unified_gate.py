"""Unified Verify-Gate — 一次 LLM 完成质量判断 + finding 识别 + decision + repair_action。

REFACTOR §5.3:合并原 gate.py 的 judge 复核(LLM #1) + HIGH finding 检查(规则) +
transition.py 的 decide_transition(if/else)为一次 LLM 调用。

**洞察**(§5.3.1):judge 看"合不合格"时已经在看失败原因和证据,让它同时判"怎么救"
几乎零成本,且能让"怎么救"看到完整证据(findings / history_playbook)。

**契约**:返回 dict 必须包含 planner.py 裸下标读的字段:
- ``decision`` ∈ {"advance", "retry", "fail"}  (总是裸下标)
- ``round`` int                                 (retry 路径裸下标,必填)
- ``retry_limit`` int                           (retry 路径裸下标,必填)
- ``reason`` str                                (fail 路径裸下标,必填)
- ``repair_action`` dict | None                 (retry 时供 build_repair_action 消费)

**Fallback**(§5.3.3):LLM 失败时降级。区分两种失败:
- 检测到 HIGH finding 存在(查 DB)→ escalate 转人(不掩盖质量问题)
- 纯 LLM 基础设施抖动(超时/坏JSON)→ retry(不打扰人)
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Literal, Optional

from pydantic import BaseModel

from ...infra.llm_client import get_llm

log = logging.getLogger("story-lifecycle.unified_gate")


# ---- Pydantic schema(供 invoke_structured)----


class RepairAction(BaseModel):
    """修复动作(§5.3.4 字段映射:替旧 transition_decision)。"""

    kind: Literal["retry", "swap_approach", "insert_rescue_stage", "escalate"]
    reason: str
    # swap 时模型基于 playbook 指定(替硬编码 _SWAP_ADAPTER_ORDER 环形轮转)
    new_adapter: Optional[str] = None
    # insert_rescue_stage 时指定救援 stage
    rescue_stage: Optional[str] = None


class VerifyGateDecision(BaseModel):
    """一次 LLM 决策的完整结构。"""

    verdict: Literal["pass", "rework"]
    decision: Literal["advance", "retry", "fail"]
    findings: list[dict] = []
    reason: str = ""
    repair_action: Optional[RepairAction] = None


# ---- 主函数 ----


def run_unified_verify_gate(
    *,
    story_key: str,
    stage: str,
    workspace: str,
    context: dict,
    quality_cfg: dict | None = None,
    max_retries: int = 2,
    done_data: dict | None = None,
    adapter_name: str = "",
    retry_count: int = 1,
) -> dict:
    """一次 LLM:质量判断 + finding 识别 + decision + repair_action。

    Args:
        story_key: 当前 story。
        stage: 当前阶段(应为 "verify")。
        workspace: 工作区根。
        context: story context_json 解析后的 dict(取 task_type / verify_round 等)。
        quality_cfg: 质量配置(保留兼容,当前未深度使用)。
        max_retries: gate 重试上限(默认 2)。
        done_data: verify stage 的 done.json 解析(summary/files_changed 等)。
        adapter_name: 当前跑 verify 的 adapter(claude/codex/kimi)。
        retry_count: 当前修复轮次(默认 1)。

    Returns:
        dict,契约见模块 docstring。LLM 失败时走 fallback(§5.3.3)。
    """
    done_data = done_data or {}
    task_type = context.get("task_type", "")

    # 1. 组装完整证据包
    from ...infra.db import models as db

    open_findings = []
    try:
        open_findings = db.get_open_findings(story_key, min_severity="high")
    except Exception as exc:
        log.warning("[%s] get_open_findings failed: %s", story_key, exc)

    history_playbook = _load_playbook_for_verify(workspace, task_type)

    evidence = {
        "story_key": story_key,
        "task_type": task_type,
        "done_summary": done_data.get("summary", ""),
        "files_changed": done_data.get("files_changed") or [],
        "test_report_path": done_data.get("test_report_path"),
        "open_high_findings": [
            {"severity": f.get("severity"), "category": f.get("category"),
             "description": f.get("description"), "location": f.get("location")}
            for f in open_findings
        ],
        "history_playbook": history_playbook,
        "retry_count": retry_count,
        "max_retries": max_retries,
        "current_adapter": adapter_name,
        "available_adapters": ["claude", "codex", "kimi"],
    }

    # 2. 一次 LLM 调用
    llm = get_llm()
    if not llm.api_key:
        log.warning("[%s] no LLM api_key, falling back", story_key)
        return _fallback_gate_decision(evidence, db, story_key)

    prompt = _build_unified_gate_prompt(evidence)
    try:
        decision = llm.invoke_structured(
            prompt, VerifyGateDecision, temperature=0.1, timeout=90
        )
        result = decision.model_dump()
        # 确保 planner 裸下标读的字段都在
        result.setdefault("round", retry_count)
        result.setdefault("retry_limit", max_retries)
        result.setdefault("reason", result.get("reason") or "verify-gate decision")
        # 记录 gate 事件(供 reflect 沉淀)
        _log_gate_event(db, story_key, stage, result, open_findings)
        return result
    except Exception as exc:
        log.warning("[%s] unified gate LLM failed, falling back: %s", story_key, exc)
        return _fallback_gate_decision(evidence, db, story_key)


# ---- Fallback(§5.3.3)----


def _fallback_gate_decision(
    evidence: dict, db, story_key: str
) -> dict:
    """LLM 不可用时降级。区分两种失败(§5.3.3):

    - 检测到 HIGH finding 存在(查 DB)→ escalate 转人(不掩盖质量问题)
    - 纯 LLM 基础设施抖动(超时/坏JSON)→ retry(不打扰人)

    防止把真实质量问题(安全漏洞/数据丢失)当基础设施抖动盲目 retry。
    """
    retry_count = evidence.get("retry_count", 1)
    max_retries = evidence.get("max_retries", 2)
    open_high = evidence.get("open_high_findings") or []

    base = {
        "round": retry_count,
        "retry_limit": max_retries,
        "verdict": "rework",
    }

    # 1. HIGH finding 存在 → 不盲目 retry,直接转人
    if open_high:
        return {
            **base,
            "decision": "fail",
            "reason": f"HIGH finding 存在({len(open_high)}条),转人审查",
            "repair_action": {
                "kind": "escalate",
                "reason": f"HIGH findings: {[f.get('description','')[:80] for f in open_high[:3]]}",
            },
            "findings": open_high,
        }

    # 2. 无 HIGH finding → 判断是否超限
    if retry_count >= max_retries:
        return {
            **base,
            "decision": "fail",
            "reason": f"retry 超限({retry_count}/{max_retries}),LLM 不可用时转人",
            "repair_action": {
                "kind": "escalate",
                "reason": "LLM 抖动 + retry 超限",
            },
        }

    # 3. 默认 retry(基础设施抖动)
    return {
        **base,
        "decision": "retry",
        "reason": "LLM 基础设施抖动,默认重试",
        "repair_action": {
            "kind": "retry",
            "reason": "LLM 不可用,同 adapter 重试",
        },
    }


# ---- Prompt 构建 ----


def _build_unified_gate_prompt(evidence: dict) -> str:
    """构建 unified gate 的 LLM prompt。"""
    findings_text = ""
    open_high = evidence.get("open_high_findings") or []
    if open_high:
        findings_lines = []
        for f in open_high:
            findings_lines.append(
                f"  - [{f.get('severity','?')}] {f.get('category','?')}: "
                f"{f.get('description','')} @ {f.get('location','?')}"
            )
        findings_text = "\n**未解决的 HIGH finding:**\n" + "\n".join(findings_lines)
    else:
        findings_text = "\n**未解决的 HIGH finding:** 无"

    playbook_text = evidence.get("history_playbook") or "（无历史经验,冷启动期）"
    files_text = ", ".join(evidence.get("files_changed") or []) or "（无）"

    return f"""你是 verify 阶段的质量门卫。基于以下证据,一次决策:

## 证据
- **Story:** {evidence.get('story_key','')} (task_type: {evidence.get('task_type','未知')})
- **verify 产出摘要:** {evidence.get('done_summary','（无摘要）')}
- **变更文件:** {files_text}
{findings_text}
- **当前 adapter:** {evidence.get('current_adapter','?')}
- **可选 adapter:** {', '.join(evidence.get('available_adapters',[]))}
- **修复轮次:** {evidence.get('retry_count',1)}/{evidence.get('max_retries',2)}

## 历史经验(playbook,参考但不盲从)
{playbook_text}

## 你的决策
基于产出物质量和历史经验,决定:
1. **verdict**: pass(合格)还是 rework(需返工)
2. **decision**: advance(通过进下一阶段)/ retry(修复后重跑)/ fail(没救了转人)
3. 如果 retry,**repair_action**: 选哪种修复?
   - retry: 同 adapter 重跑(适合偶发失败)
   - swap_approach: 换 adapter(历史经验显示换有效时——指定 new_adapter)
   - insert_rescue_stage: 插救援 stage(缺依赖等,指定 rescue_stage)
   - escalate: 转人(质量问题持续/超限)
4. **findings**: 你识别到的质量问题(如果有)

**纪律:**
- 有 HIGH finding 未解决时,倾向 fail/escalate,别盲目 retry 掩盖质量问题。
- 历史 playbook 显示"换 adapter 成功"时,优先 swap_approach 并指定 new_adapter。
- retry 超限时 escalate。

输出 JSON:
```json
{{
  "verdict": "pass|rework",
  "decision": "advance|retry|fail",
  "reason": "简短理由",
  "findings": [{{"severity":"...","category":"...","description":"..."}}],
  "repair_action": {{
    "kind": "retry|swap_approach|insert_rescue_stage|escalate",
    "reason": "...",
    "new_adapter": "claude|codex|kimi (仅 swap_approach)",
    "rescue_stage": "... (仅 insert_rescue_stage)"
  }}
}}
```"""


def _load_playbook_for_verify(workspace: str, task_type: str) -> str:
    """读当前 task_type 的历史 playbook(阶段1 产出),喂给 verify-gate 作 context。

    路径: <workspace>/.story/knowledge/playbooks/<task_type>/*.md
    冷启动期(task_type 子目录不存在/为空)→ 返回空,不崩。
    """
    if not task_type:
        return ""
    try:
        playbooks_dir = Path(workspace) / ".story" / "knowledge" / "playbooks" / task_type
        if not playbooks_dir.exists():
            return ""
        parts = []
        for f in sorted(playbooks_dir.glob("*.md")):
            content = f.read_text(encoding="utf-8")[:600]  # 截断防爆
            parts.append(f"### {f.stem}\n{content}")
        return "\n\n".join(parts) if parts else ""
    except Exception:
        return ""


def _log_gate_event(db, story_key: str, stage: str, result: dict, findings: list) -> None:
    """记录 gate_decision 事件(供 reflect 沉淀,§5.1.1 消费)。"""
    try:
        repair = result.get("repair_action") or {}
        db.log_event(
            story_key, stage, "gate_decision",
            {
                "decision": result.get("decision"),
                "verdict": result.get("verdict"),
                "pass": result.get("decision") == "advance",
                "reason": result.get("reason", ""),
                "repair_kind": repair.get("kind"),
                "new_adapter": repair.get("new_adapter"),
                "findings_count": len(findings),
            },
        )
    except Exception as exc:
        log.warning("[%s] _log_gate_event failed: %s", story_key, exc)
