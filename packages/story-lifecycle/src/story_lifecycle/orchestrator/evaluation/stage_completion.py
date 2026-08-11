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
from pathlib import Path
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
    llm = None
    db_module = None


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

    # 迭代 2（P6-JUDGE）守卫：产物「找不到」≠「写得差」。
    # round 3 Bug #6：story/*/spec.md（glob）落地判定命中但内容读取失败 → judge
    # 误判「产出为空」→ reject→escalate 循环。此处前置拦截：文件类 artifacts
    # 声明了但内容一个都没解析到 → 直接 escalate（reason 明确为路径/落点问题，
    # 不进 reject 重试循环），等修复/人工介入。
    if artifacts:
        file_arts = [
            a for a in artifacts
            if isinstance(a, str) and a and a != "git"
        ]
        resolved_count = len(judge_ctx.get("artifacts") or [])
        # glob 形态(可能被 resolver glob 命中)与文件形态一起看:只要声明过产物
        # 且一个都没读到,就是取件问题(落地判定在 submit judge 前已通过)。
        if file_arts and resolved_count == 0:
            log.warning(
                "[%s/%s] P6 guard: artifacts declared=%s but none resolvable "
                "(workspace=%s evidence=%s) → escalate",
                story_key, stage, file_arts, workspace, bool(evidence_candidates),
            )
            return _build_p6_path_escalate(
                story_key, stage, cref, db_module, file_arts, workspace,
            )

    # F2：verify stage 跑 conformance 质检（spec vs 实现 diff），结果注入判定上下文。
    # 配置：ctx 的 conformance_check（默认 true）；失败时 fail-closed（转 escalate）。
    conformance_ev, fallback = _run_conformance_check(
        story_key, stage, workspace, ctx, done_data, judge_ctx, cref, db_module
    )
    if fallback is not None:
        return fallback

    # F2b：verify stage 跑外部测试 provider（设计 10 R8 接线）。2026-08-06
    # real-run 1068018：design 12 收敛后 run_unified_verify_gate 无调用方,
    # provider 成孤儿 —— verify 的 journey 执行证据永远进不了 judge 上下文,
    # judge 只能看到 agent 自述(静态核对→连续 reject/escalate)。
    external_verify_ev = ""
    if stage == "verify":
        external_verify_ev = _run_external_verify_evidence(
            story_key, stage, workspace, done_data, ctx, db_module
        )

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
        external_verify_ev=external_verify_ev,
    )
    llm_out = _call_judge_llm(llm, prompt, story_key, stage, cref, db_module)
    if llm_out is None:
        return _fallback_decision(
            story_key, stage, cref, db_module, reason="LLM 调用失败"
        )
    quality, target, summary, reason, findings, repair, llm_model = llm_out

    quality, target, reason = _normalize_judge_decision(
        quality, target, reason, story_key, stage, lifecycle_state, db_module
    )

    # 4. 落 orchestrator_decision(审计,无状态编排前提)
    # 迭代 3 G4：action_payload 带 findings（severity 化 conf_findings + judge 自产合并）
    # 与 repair_action——timeline.py gate-history 合并段透传，QualityPanel 生产可见明细。
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
                "findings": (judge_ctx.get("conformance_findings") or []) + (findings or []),
                "repair_action": repair,
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
                conformance_ev = (
                    f"（conformance 跳过: {conformance_result.skip_reason}）"
                )
            else:
                conf_findings = inject_conformance_findings(conformance_result)
                judge_ctx["conformance"] = conformance_result.model_dump()
                judge_ctx["conformance_findings"] = conf_findings
                sev = (
                    "HIGH"
                    if conf_findings and conf_findings[0]["severity"] == "HIGH"
                    else "OK"
                )
                log.info(
                    "[%s/%s] conformance 检查: alignment=%s coverage=%s scope_drift=%s "
                    "(ref=%s) → %s",
                    story_key, stage,
                    conformance_result.alignment, conformance_result.coverage,
                    conformance_result.scope_drift, conformance_result.reference_type,
                    sev,
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


def _run_external_verify_evidence(
    story_key: str,
    stage: str,
    workspace: str,
    done_data: dict,
    ctx: dict,
    db_module,
) -> str:
    """verify stage 跑外部测试 provider，把真实执行证据格式化成 judge prompt 段落。

    设计 10 R8 接线（2026-08-06 real-run 1068018 修复）：design 12 收敛后
    run_unified_verify_gate 无调用方 → provider 成孤儿。这里在 stage_completion
    （verify 阶段的实际裁判）补上调用：证据进 judge 上下文，judge 才能基于真实
    执行结果而非 agent 自述做决定。

    - provider 未配置 / 返回 None（异步起跑模式）→ 空段落，judge 维持 LLM-only。
    - FAIL → 同时落 finding（source=test_failure，open_findings 会喂进证据）。
    - 任何异常 → 空段落（provider 容错哲学，不阻断 judge）。

    Returns: 格式化证据文本（空串 = 无外部测试参与）。
    """
    try:
        ext = _run_external_verify(story_key, workspace, done_data, ctx)
    except Exception as exc:  # noqa: BLE001 — provider 容错，不阻断 judge
        log.warning("[%s/%s] external verify 失败(忽略): %s", story_key, stage, exc)
        return ""
    if ext is None:
        return ""

    lines = [f"- 结果: {'PASS' if ext.passed else 'FAIL'}", f"- 摘要: {ext.summary[:300]}"]
    if ext.evidence_ref:
        lines.append(f"- 证据: {ext.evidence_ref}")
    if ext.evidence:
        import json as _json

        lines.append(f"- 明细: {_json.dumps(ext.evidence, ensure_ascii=False)[:1000]}")
    for f in ext.findings:
        lines.append(
            f"- finding: {f.get('scenario') or ''} {f.get('status') or ''} "
            f"{(f.get('detail') or f.get('description') or '')[:200]}"
        )

    if not ext.passed:
        try:
            from .quality import record_finding

            for f in ext.findings:
                record_finding(
                    story_key,
                    stage,
                    {
                        "source": "test_failure",
                        "severity": f.get("severity") or "high",
                        "category": "test_failure",
                        "description": (
                            f.get("detail") or f.get("description") or ext.summary
                        ),
                        "location": f.get("scenario"),
                    },
                )
        except Exception as exc:  # noqa: BLE001 — finding 记录失败不阻断
            log.warning("[%s/%s] record_finding failed (non-fatal): %s", story_key, stage, exc)

    return "\n".join(lines)


def _run_external_verify(
    story_key: str, workspace: str, done_data: dict, context: dict
):
    """如果配了 verify_provider，执行（或起跑）外部测试。

    迭代 3 G5：从 unified_gate 迁入（唯一消费者 stage_completion），孤儿模块删除。

    修订点 R8 接线：把规划产物 _agent_actions 合入 done_data，provider 据此读
    selected_scenarios（selected_scenarios 存在 ctx["_agent_actions"]，不在
    done.json 里——不合入 provider 将永远拿不到）。
    """
    try:
        from ...infra.config import get_config
        from ..verify_providers import load_verify_provider

        config = get_config()
        provider = load_verify_provider(config)
        if provider is None:
            return None
        done_data = {
            **done_data,
            "_agent_actions": context.get("_agent_actions", []),
        }
        return provider.verify(story_key, workspace, "verify", done_data)
    except Exception as exc:
        log.warning("[%s] external verify 执行失败，忽略: %s", story_key, exc)
        return None


def _load_playbook_for_verify(workspace: str, task_type: str) -> str:
    """读当前 task_type 的历史 playbook(阶段1 产出),喂给 verify-gate 作 context。

    迭代 3 G5：从 unified_gate 迁入（唯一消费者 stage_completion），孤儿模块删除。

    路径: <workspace>/.story/knowledge/playbooks/<task_type>/*.md
    冷启动期(task_type 子目录不存在/为空)→ 返回空,不崩。
    """
    if not task_type:
        return ""
    try:
        playbooks_dir = (
            Path(workspace) / ".story" / "knowledge" / "playbooks" / task_type
        )
        if not playbooks_dir.exists():
            return ""
        parts = []
        for f in sorted(playbooks_dir.glob("*.md")):
            content = f.read_text(encoding="utf-8")[:600]  # 截断防爆
            parts.append(f"### {f.stem}\n{content}")
        return "\n\n".join(parts) if parts else ""
    except Exception:
        return ""


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


def _build_p6_path_escalate(
    story_key: str, stage: str, cref: str, db_module, artifacts: list, workspace: str
) -> dict:
    """P6 守卫：产物声明了但内容解析全空 → escalate（reason 指明落点问题）。

    与 _fallback_decision 同构（不调 LLM、不推进、落决策记录）——但 reason 明确
    是「路径/落点」问题而非 LLM 基础设施问题，UI 可据此区分展示。
    """
    try:
        rid = db_module.log_decision(
            story_key,
            stage,
            "stage_completion",
            "escalate",
            reason=f"[PATH-MISS] 产物声明 {artifacts} 在 workspace={workspace} 未解析到内容（落地判定已过但内容读取为空）",
            context_ref=cref,
            action_taken="p6_path_escalate",
            llm_model="",
        )
    except Exception:  # noqa: BLE001
        rid = 0
    return {
        "quality": "escalate",
        "lifecycle_target": None,
        "summary": "",
        "reason": f"[PATH-MISS] 产物 {artifacts} 未解析到内容: workspace={workspace}",
        "findings": [],
        "repair_action": None,
        "logged_decision_id": rid,
        "context_ref": cref,
        "fallback": True,
    }


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
    external_verify_ev: str = ""


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
    external_verify_ev: str = "",
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
            external_verify_ev=external_verify_ev,
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
    external_verify_ev = req.external_verify_ev
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

## 外部测试证据(verify_provider,真实执行结果)
{external_verify_ev}

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
