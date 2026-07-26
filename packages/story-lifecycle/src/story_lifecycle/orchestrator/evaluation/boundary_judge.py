"""调度点① 边界纯判定 LLM(STEP 2.2,DESIGN §4.2 / 评审 B)。

成果物落地(check_artifacts_landed)后,本模块判完成 + 判质量。**纯判定函数 + 预注入
上下文,非 agentic**(红线 1):不给 LLM 加 read_file/query_db 工具 —— 边界输入完全有界
(PRD+成果物+决策历史),预注入和让它自读信息量一样,后者只多不可复现路径(评审 B)。

**unified_gate 并入**(§4.2):verify stage 的质量判断(原 run_unified_verify_gate)归并到
这里一次做完完成+质量。按 stage 分支:verify 走质量判定(含 findings/playbook),其他 stage
走成果物完整 + 一致性判定。

**Decider/Handler 分层**:judge_boundary 是纯 Decider(只出决策,零副作用);planner 是
Handler(执行推进/打回副作用)。judge_boundary 不更新 DB 状态(除 log_decision 审计)。

**reject 上限**(§4.9 / 评审 A2):reject 前 check_reject_budget;不允许 → 强制 escalate。

**confirm=true 不变量**(红线 4,评审 A):approve **不自动推进**。approve 后 planner 仍按
stage.confirm 走确认闸。LLM false approve 靠人确认兜底。
"""

from __future__ import annotations

import logging
from typing import Literal

from pydantic import BaseModel

log = logging.getLogger("story-lifecycle.boundary_judge")


# ---- Pydantic schema(供 invoke_structured)----


class BoundaryDecision(BaseModel):
    """边界纯判定的 LLM 输出结构。"""

    decision: Literal["approve", "reject", "escalate"]
    verdict: Literal["pass", "rework"] = "pass"
    reason: str = ""
    # verify stage 质量判定时可能识别 findings(复用 unified_gate 的字段)
    findings: list[dict] = []


# ---- 主函数(纯 Decider)----


def judge_boundary(
    *,
    story_key: str,
    stage: str,
    workspace: str,
    ctx: dict,
    artifacts: list[str],
    adapter: str = "",
    llm=None,
    db_module=None,
) -> dict:
    """调度点① 边界纯判定:成果物落地后判完成 + 判质量。

    Args:
        story_key / stage: 当前判定的 story+stage。
        workspace: 工作区根(组装上下文读成果物)。
        ctx: story context_json(取 task_type / verify_round 等)。
        artifacts: 该 stage 的成果物路径(传给 judge_context 读内容)。
        adapter: 当前 stage 的 adapter(查 session 执行轨迹)。
        llm: 注入 LLM 客户端(测试用);None 则 get_llm()。
        db_module: 注入 db(测试用);None 则延迟 import。

    Returns:
        {"decision", "reason", "verdict", "findings", "logged_decision_id", "context_ref"}
        decision ∈ {"approve", "reject", "escalate"}。
        - approve:成果物合格,planner 按 confirm 走(不自动推进)。
        - reject:成果物有缺陷,planner 回 code CLI(带 reason seed)。
        - escalate:reject 超上限 / 理由重复 / LLM 判需人 → planner paused 等人。

    LLM 不可用 → fallback:默认 approve(让人兜底,红线 4)+ 标记 unverified。
    """
    if db_module is None:
        from ...infra.db import models as db_module

    # 1. 组装上下文(预注入,非 agentic,§4.6 / §2.4)
    from ..context.judge_context import assemble_judge_context, context_ref

    judge_ctx = assemble_judge_context(
        story_key, stage, workspace, artifacts=artifacts, adapter=adapter
    )
    cref = context_ref(judge_ctx)

    # 2. LLM 调用(纯判定,无工具)
    if llm is None:
        from ...infra.llm_client import get_llm

        llm = get_llm()
    if not llm.api_key:
        log.warning("[%s/%s] no LLM api_key, fallback approve", story_key, stage)
        return _fallback_approve(story_key, stage, cref, db_module, reason="LLM 不可用,默认 approve 让人兜底")

    prompt = _build_boundary_prompt(story_key, stage, judge_ctx, ctx)
    try:
        result = llm.invoke_structured(prompt, BoundaryDecision, temperature=0.1, timeout=90)
        decision = result.decision
        reason = result.reason or f"boundary judge: {decision}"
        verdict = result.verdict
        findings = result.findings or []
        llm_model = getattr(llm, "model", "")
    except Exception as exc:
        log.warning("[%s/%s] boundary judge LLM failed, fallback approve: %s", story_key, stage, exc)
        return _fallback_approve(story_key, stage, cref, db_module, reason=f"LLM 调用失败:{exc}")

    # 3. reject 上限防护(§4.9 / 评审 A2)
    if decision == "reject":
        from .reject_budget import check_reject_budget

        budget = check_reject_budget(story_key, stage, reason, db_module=db_module)
        if not budget["allow"]:
            log.warning(
                "[%s/%s] reject 被 reject_budget 拦(force=%s, warn=%s)→ 强制 escalate",
                story_key, stage, budget["force"], budget["warn"],
            )
            decision = "escalate"
            reason = f"reject 被防打回循环拦:{budget['warn']};原 reject 理由:{reason[:120]}"

    # 4. 落 orchestrator_decision(审计)
    action_taken = _action_taken_for(decision)
    try:
        rid = db_module.log_decision(
            story_key, stage, "boundary_judge", decision,
            reason=reason, context_ref=cref,
            action_taken=action_taken,
            action_payload={"verdict": verdict, "findings_count": len(findings)},
            llm_model=llm_model,
        )
    except Exception as exc:  # noqa: BLE001 — 审计 best-effort
        log.warning("[%s/%s] log_decision failed (non-fatal): %s", story_key, stage, exc)
        rid = 0

    log.info(
        "[%s/%s] boundary judge: %s (verdict=%s) — %s",
        story_key, stage, decision, verdict, reason[:120],
    )
    return {
        "decision": decision,
        "reason": reason,
        "verdict": verdict,
        "findings": findings,
        "logged_decision_id": rid,
        "context_ref": cref,
    }


# ---- Fallback(LLM 不可用,默认 approve 让人兜底,红线 4)----


def _fallback_approve(
    story_key: str, stage: str, cref: str, db_module, *, reason: str
) -> dict:
    """LLM 不可用时默认 approve(让人确认兜底,§4.2 / 红线 4)。

    标记 unverified(决策审计里 reason 写明是 fallback),让人知道这不是 LLM 真判的。
    approve 不自动推进 —— confirm=true 的 stage 仍 paused 等人。
    """
    try:
        rid = db_module.log_decision(
            story_key, stage, "boundary_judge", "approve",
            reason=f"[FALLBACK] {reason}",
            context_ref=cref,
            action_taken="fallback_approve",
            llm_model="",
        )
    except Exception:  # noqa: BLE001
        rid = 0
    return {
        "decision": "approve",
        "reason": f"[FALLBACK] {reason}",
        "verdict": "pass",
        "findings": [],
        "logged_decision_id": rid,
        "context_ref": cref,
        "fallback": True,
    }


def _action_taken_for(decision: str) -> str:
    """决策对应的 Handler 副作用(审计用,本函数不执行副作用)。"""
    return {
        "approve": "planner 按 confirm 走(不自动推进)",
        "reject": "planner 插 retry action 回 code CLI",
        "escalate": "planner paused 等人",
    }.get(decision, "")


# ---- Prompt 构建(按 stage 分支)----


def _build_boundary_prompt(
    story_key: str, stage: str, judge_ctx: dict, story_ctx: dict
) -> str:
    """构建边界判定 prompt(纯文本,预注入上下文,非 agentic)。

    按 stage 分支:
    - verify:走质量判定(复用 unified_gate 的 findings/playbook 逻辑)。
    - 其他 stage:走成果物完整 + 一致性判定。
    """
    prd = judge_ctx.get("prd", "") or "(无 PRD)"
    artifacts = judge_ctx.get("artifacts", [])
    trace = judge_ctx.get("execution_trace", {})
    history = judge_ctx.get("decision_history", [])

    # 成果物内容
    if artifacts:
        arts_lines = []
        for a in artifacts:
            arts_lines.append(f"### 成果物:{a['path']}\n{a['content']}")
        arts_text = "\n\n".join(arts_lines)
    else:
        arts_text = "(无成果物内容,可能 code agent 未产出或路径不对)"

    # 决策历史
    if history:
        hist_lines = [
            f"  - [{h.get('trigger')}] {h.get('decision')}: {h.get('reason', '')}"
            for h in history
        ]
        hist_text = "\n".join(hist_lines)
    else:
        hist_text = "(无历史决策)"

    # 执行轨迹
    sess = trace.get("session") or {}
    trace_text = (
        f"adapter={sess.get('adapter','?')}, attempt={sess.get('attempt','?')}, "
        f"outcome={sess.get('outcome','?')}, failure_reason={sess.get('failure_reason') or '(无)'}"
    )

    # verify 专属:open findings + playbook
    verify_extra = ""
    if stage == "verify":
        from ...infra.db import models as _db

        try:
            open_findings = _db.get_open_findings(story_key, min_severity="high")
            if open_findings:
                fl = "\n".join(
                    f"  - [{f.get('severity')}] {f.get('description', '')}"
                    for f in open_findings
                )
                verify_extra += f"\n\n**未解决 HIGH finding:**\n{fl}"
            else:
                verify_extra += "\n\n**未解决 HIGH finding:** 无"
        except Exception:  # noqa: BLE001
            pass

    stage_desc = {
        "verify": "验证 + 交付(判测试报告质量 + findings)",
        "design": "方案设计(判 spec 完整性 + 与 PRD 一致)",
        "build": "编码实现(判代码改动 + 实现符合 spec)",
    }.get(stage, "本阶段产出")

    return f"""你是 story 编排的边界判定器。**纯判定,不要尝试调用任何工具**。
本阶段({stage}:{stage_desc})的成果物已落地,你判:完成 + 质量。

## PRD(需求基准)
{prd[:3000]}

## 成果物内容(本阶段产出)
{arts_text}
{verify_extra}

## 执行轨迹
{trace_text}

## 编排决策历史(本 stage)
{hist_text}

## 你的判定
基于成果物内容 vs PRD 需求 + 执行轨迹 + 历史,决定:
- **approve**:成果物完整 + 质量合格(覆盖 PRD,无明显缺陷)。注意:approve 只代表你判合格,
  人确认闸(confirm=true)仍会拦 —— 你不替人拍板。
- **reject**:成果物有**具体**缺陷(明确说出缺什么/哪里错/与 PRD 哪条不符)。
  **reject 理由必须与上次不同**(若重复,系统会强制 escalate —— 你在抖)。
- **escalate**:不确定 / 需人判断 / 质量问题持续。

**纪律:**
- 只判,不改(你不能动文件)。
- reject 必须给可执行的、与上次不同的具体理由(code agent 据此重做)。
- 有 HIGH finding 未解决时(verify),倾向 escalate。
- 不要因为"看起来还行"就 approve —— 对照 PRD 逐条。

输出 JSON:
```json
{{
  "decision": "approve|reject|escalate",
  "verdict": "pass|rework",
  "reason": "简短具体理由(reject 必须可执行且与上次不同)",
  "findings": [{{"severity":"...","category":"...","description":"..."}}]
}}
```"""
