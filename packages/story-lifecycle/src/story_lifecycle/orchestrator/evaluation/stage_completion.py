"""Stage 完成裁判 — 一次 LLM 三个决定(设计 12 改动 1)。

替换 boundary_judge + _stages_done + gate_satisfied:stage 完成后一次 LLM 调用输出
**三个决定**:

1. ``quality``: approve / reject / escalate —— 原 boundary_judge 的职责(stage 成果物质量)。
2. ``lifecycle_target``: story 现在应该处于哪个 lifecycle 状态(可跨多状态)——
   替换硬编码的 ``_stages_done`` + ``gate_satisfied``。LLM 看**所有累积产出**(不止本轮
   stage)判断满足了哪些 gate,planner 逐个状态推进,遇 ui_button 停住等人确认。
3. ``summary``: 本轮 stage 干了什么(1-3 句话,给 UI 展示)。

**Decider/Handler 分层**:judge_stage_completion 是纯 Decider(只出决策 + log_decision 审计,
零状态副作用);planner 是 Handler(执行推进/打回/暂停)。advance_lifecycle_to_target 是
共享的推进函数(planner 与 /lifecycle/advance 确认后续推都用它)。

**reject 上限**(§4.9 / 评审 A2):reject 前 check_reject_budget;不允许 → 强制 escalate。
**confirm=true 不变量**(红线 4):approve 不自动推进 —— 遇 ui_button 停住等人。
**LLM 不可用降级**:quality=approve, lifecycle_target=None, summary=""(让人确认兜底)。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Literal, Optional

from pydantic import BaseModel

log = logging.getLogger("story-lifecycle.stage_completion")

# 业务状态顺序(对齐 sourcing/lifecycle_state.py 的 5 态)。
LIFECYCLE_ORDER = ["待启动", "开发", "测试", "上线", "结项"]

# 默认最大修复轮次(stage profile 没配 max_retries 时用)。
_DEFAULT_MAX_RETRIES = 2


# ---- Pydantic schema(供 invoke_structured)----


class RepairAction(BaseModel):
    """reject 时的修复方案(对齐 unified_gate.RepairAction 的字段)。"""

    kind: Literal["retry", "swap_approach", "insert_rescue_stage", "escalate"]
    reason: str
    new_adapter: Optional[str] = None
    rescue_stage: Optional[str] = None


class StageCompletionDecision(BaseModel):
    """stage 完成后一次 LLM 裁判的输出(替换 boundary_judge + _stages_done + gate_satisfied)。

    三个决定:
    - quality: stage 成果物质量(approve/reject/escalate)。
    - lifecycle_target: story 现在应该处于哪个 lifecycle 状态(看累积产出,可跨多状态)。
      None/空串 = 不推进;否则是「最远能到哪」,planner 逐个状态推进。
    - summary: 本轮 stage 干了什么(1-3 句话,给 UI 展示)。
    """

    quality: Literal["approve", "reject", "escalate"]
    lifecycle_target: Optional[str] = None
    summary: str = ""
    reason: str = ""
    findings: list[dict] = []
    repair_action: Optional[RepairAction] = None


# ---- 主函数(纯 Decider)----


@dataclass
class JudgeRequest:
    """judge_stage_completion 的入参打包（设计 14 F2：15 参数 → 1 参数对象）。

    字段 = 原 judge_stage_completion 参数，类型照搬。
    """

    story_key: str
    stage: str
    workspace: str
    ctx: dict
    lifecycle_state: str
    done_data: dict | None = None
    cumulative_outputs: str = ""
    adapter: str = ""
    retry_count: int = 1
    story_states: dict | None = None
    artifacts: list[str] | None = None
    evidence_candidates: dict[str, list[str]] | None = None
    max_retries: int = _DEFAULT_MAX_RETRIES
    llm=None
    db_module=None


def judge_stage_completion(req: JudgeRequest) -> dict:
    """调度点①' stage 完成裁判:一次 LLM 做三个决定。

    Args:
        req: JudgeRequest——story_key / stage: 当前判定的 story+stage。
        workspace: 工作区根(组装上下文读成果物)。
        ctx: story context_json(取 task_type 等)。
        lifecycle_state: 当前 lifecycle 状态(LLM 决定 target 的起点)。
        done_data: 本轮 done.json 兼容视图(summary/files_changed 等)。
        cumulative_outputs: 所有已完成 stage 的产出摘要(collect_cumulative_outputs)。
        adapter: 当前 stage 的 adapter。
        retry_count / max_retries: 修复轮次(防打回循环的上下文)。
        story_states: source profile 的 story_states(source_loader 解析)。
        artifacts: 该 stage 声明的成果物(profile stage.artifacts),读内容判质量用。
        evidence_candidates: 成果物 evidence 候选,透传给 assemble_judge_context。
        llm / db_module: 注入(测试用);None 则延迟 import。

    Returns:
        {
            "quality", "lifecycle_target", "summary", "reason", "findings",
            "repair_action", "logged_decision_id", "fallback",
        }
        quality=reject 时 lifecycle_target 恒为 None(成果物不合格不算产出)。
    """
    story_key = req.story_key
    stage = req.stage
    workspace = req.workspace
    ctx = req.ctx
    lifecycle_state = req.lifecycle_state
    done_data = req.done_data
    cumulative_outputs = req.cumulative_outputs
    adapter = req.adapter
    retry_count = req.retry_count
    story_states = req.story_states
    artifacts = req.artifacts
    evidence_candidates = req.evidence_candidates
    max_retries = req.max_retries
    llm = req.llm
    db_module = req.db_module

    if db_module is None:
        from ...infra.db import models as db_module

    done_data = done_data or {}

    # 1. 组装质量判定上下文(预注入,非 agentic — 对齐 boundary_judge 红线)。
    from ..context.judge_context import assemble_judge_context, context_ref

    judge_ctx = assemble_judge_context(
        story_key,
        stage,
        workspace,
        artifacts=artifacts or [],
        adapter=adapter,
        evidence_candidates=evidence_candidates,
    )
    cref = context_ref(judge_ctx)

    # F2：verify stage 跑 conformance 质检（spec vs 实现 diff），结果注入判定上下文。
    # 配置：ctx 的 conformance_check（默认 true）；失败时 fail-closed（转 escalate）。
    conformance_ev, fallback = _run_conformance_check(
        story_key, stage, workspace, ctx, done_data, judge_ctx, cref, db_module
    )
    if fallback is not None:
        return fallback

    # 2. LLM 调用(纯判定,无工具)
    if llm is None:
        from ...infra.llm_client import get_llm

        llm = get_llm()
    if not llm.api_key:
        log.warning("[%s/%s] no LLM api_key, fallback escalate", story_key, stage)
        return _fallback_decision(
            story_key, stage, cref, db_module, reason="LLM 不可用,转人确认(fail-closed)"
        )

    prompt = _build_prompt(
        story_key=story_key,
        stage=stage,
        workspace=workspace,
        ctx=ctx,
        lifecycle_state=lifecycle_state,
        done_data=done_data,
        cumulative_outputs=cumulative_outputs,
        adapter=adapter,
        retry_count=retry_count,
        max_retries=max_retries,
        story_states=story_states or {},
        judge_ctx=judge_ctx,
        conformance_ev=conformance_ev,
    )
    llm_out = _call_judge_llm(
        llm, prompt, story_key, stage, cref, db_module
    )
    if llm_out is None:
        return _fallback_decision(
            story_key, stage, cref, db_module, reason="LLM 调用失败"
        )
    quality, target, summary, reason, findings, repair, llm_model = llm_out

    quality, target, reason = _normalize_judge_decision(
        quality, target, reason, story_key, stage, lifecycle_state, db_module
    )

    # 4. 落 orchestrator_decision(审计,无状态编排前提)
    try:
        rid = db_module.log_decision(
            story_key,
            stage,
            "stage_completion",
            quality,
            reason=reason,
            context_ref=cref,
            action_taken=_action_taken_for(quality),
            action_payload={
                "lifecycle_target": target,
                "summary": summary[:200],
                "findings_count": len(findings),
            },
            llm_model=llm_model,
        )
    except Exception as exc:  # noqa: BLE001 — 审计 best-effort
        log.warning(
            "[%s/%s] log_decision failed (non-fatal): %s", story_key, stage, exc
        )
        rid = 0

    log.info(
        "[%s/%s] stage completion: quality=%s target=%s — %s",
        story_key,
        stage,
        quality,
        target or "-",
        reason[:120],
    )
    return {
        "quality": quality,
        "lifecycle_target": target,
        "summary": summary,
        "reason": reason,
        "findings": findings,
        "repair_action": repair,
        "logged_decision_id": rid,
        "context_ref": cref,
    }


def _run_conformance_check(
    story_key, stage, workspace, ctx, done_data, judge_ctx, cref, db_module
):
    """verify stage 的 conformance 质检（设计15 阶段D 从 judge_stage_completion 抽出）。

    Returns: (conformance_ev, fallback_response)。fallback 非 None 表示
    fail-closed（conformance 自身失败转 escalate）。
    """
    conformance_ev = ""
    if stage == "verify" and ctx.get("conformance_check", True):
        try:
            from .conformance import check_conformance, inject_conformance_findings

            conformance_result = check_conformance(
                story_key=story_key,
                workspace=workspace,
                spec_path=done_data.get("spec_path") or "",
                diff_text=done_data.get("delivery_diff"),
                delivery_diff_path=done_data.get("delivery_diff_path"),
                files_changed=done_data.get("files_changed"),
            )
            if conformance_result.skipped:
                conformance_ev = f"（conformance 跳过: {conformance_result.skip_reason}）"
            else:
                conf_findings = inject_conformance_findings(conformance_result)
                judge_ctx["conformance"] = conformance_result.model_dump()
                judge_ctx["conformance_findings"] = conf_findings
                sev = (
                    "HIGH"
                    if conf_findings and conf_findings[0]["severity"] == "HIGH"
                    else "OK"
                )
                conformance_ev = (
                    f"conformance 检查: alignment={conformance_result.alignment} "
                    f"coverage={conformance_result.coverage} scope_drift={conformance_result.scope_drift} "
                    f"(ref={conformance_result.reference_type}) → {sev}\n"
                    f"  摘要: {conformance_result.summary[:200]}"
                )
        except Exception as exc:
            # fail-closed：conformance 自身失败不允许跳过检查静默放行 → escalate
            log.warning(
                "[%s/%s] conformance 失败,转 escalate: %s", story_key, stage, exc
            )
            return "", _fallback_decision(
                story_key,
                stage,
                cref,
                db_module,
                reason=f"conformance 检查失败(fail-closed): {exc}",
            )
    return conformance_ev, None


def _call_judge_llm(llm, prompt, story_key, stage, cref, db_module):
    """调 LLM 拿三决定（设计15 阶段D 从 judge_stage_completion 抽出）。

    Returns: (quality, target, summary, reason, findings, repair, llm_model)；
    失败返回 None（调用方走 fallback）。
    """
    try:
        result = llm.invoke_structured(
            prompt, StageCompletionDecision, temperature=0.1, timeout=90
        )
        quality = result.quality
        target = result.lifecycle_target
        summary = (result.summary or "").strip()
        reason = result.reason or f"stage completion: {quality}"
        findings = result.findings or []
        repair = result.repair_action.model_dump() if result.repair_action else None
        llm_model = getattr(llm, "model", "")
    except Exception as exc:
        log.warning(
            "[%s/%s] stage completion LLM failed, fallback approve: %s",
            story_key,
            stage,
            exc,
        )
        return None
    return quality, target, summary, reason, findings, repair, llm_model


def _normalize_judge_decision(
    quality, target, reason, story_key, stage, lifecycle_state, db_module
):
    """规范化三决定（设计15 阶段D 从 judge_stage_completion 抽出）。

    - lifecycle_target 必须是合法状态且 != 当前，否则视为不推进。
    - 纪律：quality != approve 时 lifecycle_target 恒为 None。
    - reject 上限防护（§4.9 / 评审 A2）：超预算强制 escalate。
    """
    if target and target not in LIFECYCLE_ORDER:
        log.warning(
            "[%s/%s] judge returned invalid lifecycle_target=%r, treating as None",
            story_key,
            stage,
            target,
        )
        target = None
    if target == lifecycle_state:
        target = None

    # 纪律:quality != approve 时 lifecycle_target 恒为 None(不合格不算产出)。
    if quality != "approve":
        target = None

    # 3. reject 上限防护(§4.9 / 评审 A2)
    if quality == "reject":
        from .reject_budget import check_reject_budget

        budget = check_reject_budget(story_key, stage, reason, db_module=db_module)
        if not budget["allow"]:
            log.warning(
                "[%s/%s] reject 被 reject_budget 拦(force=%s, warn=%s)→ 强制 escalate",
                story_key,
                stage,
                budget["force"],
                budget["warn"],
            )
            quality = "escalate"
            reason = (
                f"reject 被防打回循环拦:{budget['warn']};原 reject 理由:{reason[:120]}"
            )
    return quality, target, reason


# ---- Fallback(LLM 不可用)----


def _fallback_decision(
    story_key: str, stage: str, cref: str, db_module, *, reason: str
) -> dict:
    """LLM 不可用时降级（F1 fail-closed）：escalate + 不推进 + 标记摘要。

    迭代 1 改动：quality 由 approve 改为 escalate——LLM 基础设施失败不得静默放行，
    转人工确认（红线 4：人不确认不推进）。lifecycle_target=None 不自动跳状态。
    """
    try:
        rid = db_module.log_decision(
            story_key,
            stage,
            "stage_completion",
            "escalate",
            reason=f"[FALLBACK] {reason}",
            context_ref=cref,
            action_taken="fallback_escalate",
            llm_model="",
        )
    except Exception:  # noqa: BLE001
        rid = 0
    return {
        "quality": "escalate",
        "lifecycle_target": None,
        "summary": "",
        "reason": f"[FALLBACK] {reason}",
        "findings": [],
        "repair_action": None,
        "logged_decision_id": rid,
        "context_ref": cref,
        "fallback": True,
    }


def _action_taken_for(quality: str) -> str:
    """决策对应的 Handler 副作用(审计用,本函数不执行副作用)。"""
    return {
        "approve": "planner 按 lifecycle_target 推进(遇 ui_button 停住)",
        "reject": "planner 插 retry action 回 code CLI",
        "escalate": "planner paused 等人",
    }.get(quality, "")


# ---- 累积产出收集 ----


def collect_cumulative_outputs(workspace: str, story_key: str, actions: list) -> str:
    """收集所有已完成 stage 的产出摘要(喂给 LLM 判 lifecycle_target)。

    优先从 ``artifact_declared`` event 读每个 launch stage 的摘要（归一化真相源，
    1068018 事故修复）；event 读不到时兜底读 done.json 兼容视图。best-effort。
    """
    from pathlib import Path

    from ...infra.db import models as db

    done_dir = Path(workspace) / ".story" / "done" / story_key
    lines = []
    for action in actions or []:
        if action.get("action") != "launch":
            continue
        _st = action.get("stage", "")
        if not _st:
            continue
        # 1. 优先从 declare event 读
        declared = None
        try:
            declared = db.get_latest_declare(story_key, _st, since_version=-1)
        except Exception:  # noqa: BLE001
            pass
        data = {}
        if declared:
            data = {
                "summary": declared.get("summary", ""),
                "files_changed": declared.get("files_changed") or [],
            }
        else:
            # 2. 兜底 done.json 兼容视图
            dj = done_dir / f"{_st}.json"
            if not dj.exists():
                continue
            try:
                data = json.loads(dj.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001
                data = {}
        _sum = (data.get("summary") or "").strip() or "（无摘要）"
        _fc = data.get("files_changed") or []
        line = f"- {_st}(已完成): {_sum}"
        if _fc:
            line += f";变更文件: {', '.join(str(f) for f in _fc[:10])}"
        lines.append(line)
    return "\n".join(lines) if lines else "（无已完成 stage 产出）"


# ---- Prompt 构建 ----


@dataclass
class JudgePromptRequest:
    """_build_prompt 的入参打包（设计15 阶段D：13 参数 → 1 参数对象）。

    字段 = 原 _build_prompt 参数，类型照搬。纯数据容器，不改逻辑。
    """

    story_key: str
    stage: str
    workspace: str
    ctx: dict
    lifecycle_state: str
    done_data: dict
    cumulative_outputs: str
    adapter: str
    retry_count: int
    max_retries: int
    story_states: dict
    judge_ctx: dict
    conformance_ev: str = ""


def _build_prompt(
    *,
    story_key: str,
    stage: str,
    workspace: str,
    ctx: dict,
    lifecycle_state: str,
    done_data: dict,
    cumulative_outputs: str,
    adapter: str,
    retry_count: int,
    max_retries: int,
    story_states: dict,
    judge_ctx: dict,
    conformance_ev: str = "",
) -> str:
    """组装 stage 完成裁判 prompt(设计 §1.3)。

    设计15 阶段D：薄转发到 _build_prompt_req（JudgePromptRequest 单参数）。
    """
    return _build_prompt_req(
        JudgePromptRequest(
            story_key=story_key,
            stage=stage,
            workspace=workspace,
            ctx=ctx,
            lifecycle_state=lifecycle_state,
            done_data=done_data,
            cumulative_outputs=cumulative_outputs,
            adapter=adapter,
            retry_count=retry_count,
            max_retries=max_retries,
            story_states=story_states,
            judge_ctx=judge_ctx,
            conformance_ev=conformance_ev,
        )
    )


def _build_prompt_req(req: JudgePromptRequest) -> str:
    """组装 stage 完成裁判 prompt 主体（原 _build_prompt 正文，设计15 阶段D 迁入）。"""
    story_key = req.story_key
    stage = req.stage
    workspace = req.workspace
    ctx = req.ctx
    lifecycle_state = req.lifecycle_state
    done_data = req.done_data
    cumulative_outputs = req.cumulative_outputs
    adapter = req.adapter
    retry_count = req.retry_count
    max_retries = req.max_retries
    story_states = req.story_states
    judge_ctx = req.judge_ctx
    conformance_ev = req.conformance_ev
    task_type = ctx.get("task_type", "") or "未知"
    prd = judge_ctx.get("prd", "") or "(无 PRD)"
    artifacts = judge_ctx.get("artifacts", [])
    history = judge_ctx.get("decision_history", [])

    # Lifecycle 状态定义(story_states YAML)
    states_yaml = "(无状态机定义,story 退化扁平)"
    if story_states:
        try:
            import yaml

            states_yaml = yaml.safe_dump(
                story_states, allow_unicode=True, sort_keys=False
            ).strip()
        except Exception:  # noqa: BLE001
            states_yaml = str(story_states)[:2000]

    # Lifecycle gate 定义(deliverables.py 的 LIFECYCLE_GATES)
    gates_text = "(无 gate 定义)"
    try:
        from ...sourcing.deliverables import LIFECYCLE_GATES

        gate_lines = [
            f"{f}→{t}: {list(req)}" for (f, t), req in LIFECYCLE_GATES.items()
        ]
        gates_text = "\n".join(gate_lines) if gate_lines else "(无)"
    except Exception:  # noqa: BLE001
        pass

    # 本轮产出
    files_text = ", ".join(str(f) for f in (done_data.get("files_changed") or []))
    files_text = files_text or "（无）"
    done_summary = (done_data.get("summary") or "").strip() or "（无摘要）"

    # 成果物内容(截断,judge 质量用)
    arts_text = ""
    if artifacts:
        arts_lines = []
        for a in artifacts:
            arts_lines.append(f"### 成果物:{a['path']}\n{a['content']}")
        arts_text = "\n\n".join(arts_lines)
    else:
        arts_text = "(无成果物内容可读)"

    # 决策历史
    hist_text = "（无历史决策）"
    if history:
        hist_lines = [
            f"  - [{h.get('trigger')}] {h.get('decision')}: {h.get('reason', '')}"
            for h in history
        ]
        hist_text = "\n".join(hist_lines)

    # 历史经验(playbook)
    try:
        from .unified_gate import _load_playbook_for_verify

        playbook = _load_playbook_for_verify(workspace, task_type)
    except Exception:  # noqa: BLE001
        playbook = ""
    playbook_text = playbook or "（无历史经验,冷启动期）"

    return f"""你是 story-lifecycle 的 stage 完成裁判。stage 刚跑完,基于产出做三个决定。

## Story 信息
- Story: {story_key}
- task_type: {task_type}
- 当前 lifecycle 状态: {lifecycle_state}

## Lifecycle 状态定义(source profile)
{states_yaml}

## Lifecycle gate 定义(转换需要满足的成果物)
{gates_text}

## 当前 stage
- stage: {stage}
- adapter: {adapter}
- 修复轮次: {retry_count}/{max_retries}

## 本轮 stage 产出
- 摘要: {done_summary}
- 变更文件: {files_text}

## 成果物内容(质量判定依据)
{arts_text}

## 额外质检证据(conformance: 需求↔实现吻合度)
{conformance_ev}

## 累积产出(所有已完成的 stage,判 lifecycle_target 的依据)
{cumulative_outputs}

## 历史经验(playbook,参考但不盲从)
{playbook_text}

## 编排决策历史(本 stage,reject 理由不得与上次重复)
{hist_text}

## 你的三个决定

1. **quality**: approve / reject / escalate
   - approve:成果物合格,可以继续
   - reject:成果物有缺陷,需要回 code CLI 重做(附 repair_action)
   - escalate:质量问题超限/没救了 → 转人

2. **lifecycle_target**: story 现在应该处于哪个 lifecycle 状态?
   - 看**累积产出**,判断满足了哪些 gate(Lifecycle gate 定义)
   - 可能跨多个状态:single-pass 的 verify 一次产出所有东西 → target=结项
   - 如果当前状态的目标还没达成 → target=null
   - 你判的是「最远能到哪」,planner 会逐个状态推进,遇到 ui_button 的 confirm 会停住等人确认

3. **summary**: 本轮 stage 干了什么?(1-3 句话,给用户看)

## 纪律
- quality=reject 时,lifecycle_target 应为 null(成果物不合格不算产出)
- 有 HIGH finding 未解决时,倾向 reject/escalate
- reject 理由必须与上次不同(重复会被系统强制 escalate)
- 历史 playbook 显示「换 adapter 成功」时,repair_action 用 swap_approach
- 不要因为"看起来还行"就 approve —— 对照 PRD 逐条:
{prd[:3000]}

输出 JSON:
```json
{{
  "quality": "approve|reject|escalate",
  "lifecycle_target": "开发|测试|上线|结项|null",
  "summary": "本轮干了什么...",
  "reason": "判断理由",
  "findings": [{{"severity":"...","category":"...","description":"..."}}],
  "repair_action": {{"kind":"retry|swap_approach|insert_rescue_stage|escalate","reason":"...","new_adapter":"...","rescue_stage":"..."}}
}}
```"""


# ---- Lifecycle 推进(planner + /lifecycle/advance 共享)----


def advance_lifecycle_to_target(
    *,
    story_key: str,
    ctx: dict,
    current: str,
    target: str,
    story_states: dict,
    db_module=None,
) -> dict:
    """从 current 逐个状态推进到 target,遇到 confirm=ui_button 停住等人。

    设计 §1.5:LLM 已判断 target 可达(看了所有累积产出),所以这里**不重新检查
    gate_satisfied**,只按 confirm 规则决定自动转还是停住。

    - confirm.type == ui_button → 停住:写 ``_story_state_gate``(带 final_target,
      用户确认后 /lifecycle/advance 续推)+ sm_pause。
    - confirm.type ∈ {none, config} → 自动推进(ctx._lifecycle_state + DB 同步)。

    Returns:
        {"new_state": str, "paused_for_confirm": bool}
    """
    if db_module is None:
        from ...infra.db import models as db_module

    if current not in LIFECYCLE_ORDER or target not in LIFECYCLE_ORDER:
        log.warning(
            "[%s] advance_lifecycle_to_target invalid: current=%r target=%r",
            story_key,
            current,
            target,
        )
        return {"new_state": current, "paused_for_confirm": False}
    if target == current:
        return {"new_state": current, "paused_for_confirm": False}

    from ...sourcing.state_machine import pause as sm_pause

    cur_idx = LIFECYCLE_ORDER.index(current)
    tgt_idx = LIFECYCLE_ORDER.index(target)

    paused = False
    for i in range(cur_idx, tgt_idx):
        from_state = LIFECYCLE_ORDER[i]
        to_state = LIFECYCLE_ORDER[i + 1]
        state_def = (story_states or {}).get(from_state) or {}
        confirm = state_def.get("confirm") or {}
        ctype = confirm.get("type", "none")

        if ctype == "ui_button":
            # 停住等人确认
            ctx["_story_state_gate"] = {
                "from": from_state,
                "to": to_state,
                "awaiting_confirm": True,
                "label": confirm.get("label", f"进入{to_state}"),
                # 记住最终 target,用户确认后继续推进
                "final_target": target,
            }
            sm_pause(story_key, ctx_updates=ctx)
            try:
                db_module.log_event(
                    story_key,
                    "",
                    "story_state_gate_reached",
                    {"from": from_state, "to": to_state, "final_target": target},
                )
            except Exception:  # noqa: BLE001
                pass
            log.info(
                "[%s] story state gate: LLM target=%s, paused at %s→%s awaiting confirm",
                story_key,
                target,
                from_state,
                to_state,
            )
            paused = True
            break
        # none / config → 自动推进(旧的确认闸标记已处理,一并清除)
        ctx["_lifecycle_state"] = to_state
        ctx.pop("_story_state_gate", None)
        try:
            db_module.update_story(
                story_key,
                lifecycle_state=to_state,
                context_json=json.dumps(ctx, ensure_ascii=False),
            )
            db_module.log_event(
                story_key,
                "",
                "story_state_transition",
                {"from": from_state, "to": to_state, "auto": True},
            )
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "[%s] lifecycle auto-advance %s→%s persist failed: %s",
                story_key,
                from_state,
                to_state,
                exc,
            )
        log.info(
            "[%s] story state auto-advanced (LLM target): %s → %s",
            story_key,
            from_state,
            to_state,
        )

    return {
        "new_state": ctx.get("_lifecycle_state", current),
        "paused_for_confirm": paused,
    }


__all__ = [
    "StageCompletionDecision",
    "judge_stage_completion",
    "collect_cumulative_outputs",
    "advance_lifecycle_to_target",
    "LIFECYCLE_ORDER",
]
