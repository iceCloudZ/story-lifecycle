"""LangGraph node implementations — plan, execute, poll, review, advance, skip, retry, fail."""

import json
import logging
from pathlib import Path

from langgraph.errors import GraphInterrupt
from langgraph.types import interrupt

from ...db import models as db
from ...terminal import ttyd
from .. import planner
from .. import router as llm_router
from ..notify import send as notify
from ..observability import (
    log_node_error,
    log_route_decision,
    log_prompt_context,
    log_dod_check,
)
from ..evaluator_loop import AdversarialConfig
from ..paths import (
    stage_done_file,
    context_dir,
    plan_file,
    review_file,
    done_snapshot_file,
    malformed_done_file,
)

from .state import StoryState, STORY_HOME, MAX_REVIEW_RETRIES
from .profile_loader import load_profile, get_stage_config
from .stage_resolver import _is_cancelled, _block_for_planner, resolve_next_stage
from .subtask_delegate import _delegate_subtasks
from .knowledge import _check_pattern_recurrence, _update_knowledge
from .json_helpers import robust_json_parse
from .prompt_renderer import (
    _build_plan_executor_prompt,
    _render_prompt,
)

log = logging.getLogger("story-lifecycle.nodes")


def plan_stage_node(state: StoryState) -> StoryState:
    """架构师/PM 角色：规划当前阶段。

    不调用其他节点。skip 通过 conditional edge 路由。
    """
    if _is_cancelled(state):
        return state

    stage = state["current_stage"]
    profile = state.get("profile", "minimal")
    cfg = get_stage_config(profile, stage)
    workspace = state["workspace"]
    story_key = state["story_key"]

    # Guard: execution_count exceeded max_retries — pause for human decision
    max_retries = cfg.get("max_retries", 3)
    if state.get("execution_count", 0) >= max_retries:
        state["_pre_routed_action"] = "wait_confirm"
        state["last_error"] = (
            f"执行次数 ({state['execution_count']}) 已达上限 ({max_retries})，等待人工决定"
        )
        return state

    # Clear stale routing state from previous graph cycles
    state.pop("_next_action", None)
    state.pop("_pre_routed_action", None)

    # Trigger Condenser if needed
    try:
        compressed_path = planner.compress_context(workspace, story_key, stage)
        if compressed_path:
            state["context"]["knowledge_path"] = compressed_path
            db.log_event(story_key, stage, "condense", {"output": compressed_path})
    except Exception as e:
        log.warning(f"Condenser failed: {e}")

    # --- Adversarial plan loop ---
    try:
        profile_cfg = load_profile(profile)
        adv_cfg = AdversarialConfig.from_profile(profile_cfg)
        if adv_cfg.plan_loop_enabled(stage):
            from ...orchestrator.graph import emit_plan_done
            from ..evaluator_loop import run_plan_loop

            adapters = ["claude"]
            loop_result = run_plan_loop(state, adv_cfg, adapters)

            # Skip path
            if (
                loop_result.decision == "pass"
                and loop_result.final_plan
                and loop_result.final_plan.get("skip")
            ):
                state["status"] = "skipping"
                state["plan_summary"] = (
                    f"跳过: {loop_result.final_plan.get('reasoning', '')}"
                )
                emit_plan_done(story_key, state["plan_summary"])
                return state

            # Only accept plan on pass decision
            if loop_result.decision == "pass" and loop_result.final_plan:
                plan = loop_result.final_plan
                pf = plan_file(workspace, story_key, stage)
                pf.parent.mkdir(parents=True, exist_ok=True)
                review_path = state.get("context", {}).get("review_path")
                review_section = ""
                if review_path:
                    rf = Path(workspace) / review_path
                    if rf.exists():
                        review_section = (
                            f"\n## 前序 Review 建议\n"
                            f"请先处理以下问题：\n{rf.read_text(encoding='utf-8')}"
                        )
                expected_outputs = cfg.get("expected_outputs", [])
                done_path = f".story/done/{story_key}/{stage}.json"

                # Build expected_outputs JSON example
                output_example = {k: "..." for k in expected_outputs}
                output_hint = ""
                if expected_outputs:
                    import json as _json

                    output_hint = (
                        f"\n## 完成信号\n"
                        f"完成后必须写入 `{done_path}`，纯 JSON（不要 markdown 代码块）：\n"
                        f"```json\n{_json.dumps(output_example, ensure_ascii=False, indent=2)}\n```\n"
                    )

                pf.write_text(
                    f"# 任务书: {stage}\n\n"
                    f"## 执行指令\n{plan.get('extra_instructions', '')}\n"
                    f"{review_section}\n"
                    f"## 约束\n"
                    f"- 所有表名、字段名、类名必须以代码或数据库定义为准；未找到时标记为假设，不得写成已存在事实\n"
                    f"- 如需澄清的需求点，写入 open_questions 字段，不要阻塞当前产出\n\n"
                    f"## 配置\n"
                    f"- 执行工具: {plan.get('adapter', 'claude')}\n"
                    f"- 执行模型: {plan.get('model', 'default')}\n\n"
                    f"{output_hint}\n"
                    f"## 决策理由\n{plan.get('reasoning', '')}\n\n"
                    f"## 路径评分\n"
                    f"当前路径评分: {plan.get('trajectory_score', 'N/A')}/1.0",
                    encoding="utf-8",
                )
                state["plan_summary"] = plan.get("summary", "")
                state["trajectory_score"] = plan.get("trajectory_score")
                state["context"]["plan_path"] = str(pf.relative_to(workspace))
                state["context"]["plan_summary"] = plan.get("summary", "")
                state["plan"] = plan
                db.log_event(
                    story_key,
                    stage,
                    "plan",
                    {
                        "adapter": plan.get("adapter"),
                        "summary": plan.get("summary", "")[:100],
                        "trajectory_score": plan.get("trajectory_score"),
                        "adversarial_loop": True,
                        "loop_rounds": loop_result.rounds,
                        "loop_decision": loop_result.decision,
                    },
                )
                summary = plan.get("summary", "")
                tool_info = (
                    f"{plan.get('adapter', 'claude')} / {plan.get('model', 'sonnet')}"
                )
                plan_text = f"✓ {summary}  [dim]({tool_info})[/]"
                emit_plan_done(story_key, plan_text)
                return state

            # Non-pass decisions: route to wait_confirm for human intervention
            if loop_result.decision in ("no_progress", "max_rounds", "fail"):
                from ...orchestrator.graph import emit_plan_done

                reason = f"Plan loop {loop_result.decision}: {loop_result.reason}"
                state["plan_summary"] = reason
                state["last_error"] = reason
                state["_pre_routed_action"] = "wait_confirm"
                emit_plan_done(story_key, f"⚠ {reason}", ok=False)
                return state
    except Exception as e:
        log.warning(f"Adversarial plan loop failed, falling back to normal: {e}")
    # --- End adversarial plan loop ---

    try:
        from ...orchestrator.graph import emit_plan_done, emit_plan_activity

        adapters = ["claude"]
        emit_plan_activity(story_key, "正在分析需求，生成执行计划...")
        plan = planner.plan_stage(state, cfg, adapters)

        if plan.get("skip"):
            state["status"] = "skipping"
            state["plan_summary"] = f"跳过: {plan.get('reasoning', '')}"
            db.log_event(
                story_key,
                stage,
                "plan",
                {"action": "skip", "reasoning": plan.get("reasoning", "")},
            )
            return state

        # Planner decided to split into sub-stories
        if plan.get("split") and plan.get("subtasks"):
            return _delegate_subtasks(state, plan)

        # Write plan task file
        pf = plan_file(workspace, story_key, stage)
        pf.parent.mkdir(parents=True, exist_ok=True)

        # Append previous review suggestions if present
        review_path = state.get("context", {}).get("review_path")
        review_section = ""
        if review_path:
            rf = Path(workspace) / review_path
            if rf.exists():
                review_section = (
                    f"\n## 前序 Review 建议\n"
                    f"请先处理以下问题：\n{rf.read_text(encoding='utf-8')}"
                )

        pf.write_text(
            f"# 任务书: {stage}\n\n"
            f"## 执行指令\n{plan.get('extra_instructions', '')}\n"
            f"{review_section}\n\n"
            f"## 配置\n"
            f"- Adapter: {plan.get('adapter', 'claude')}\n"
            f"- Provider: {plan.get('provider', 'deepseek')}\n"
            f"- Model: {plan.get('model', 'sonnet')}\n\n"
            f"## 决策理由\n{plan.get('reasoning', '')}\n\n"
            f"## 路径评分\n"
            f"当前路径评分: {plan.get('trajectory_score', 'N/A')}/1.0",
            encoding="utf-8",
        )

        state["plan_summary"] = plan.get("summary", "")
        state["trajectory_score"] = plan.get("trajectory_score")
        state["context"]["plan_path"] = str(pf.relative_to(workspace))
        state["context"]["plan_summary"] = plan.get("summary", "")
        state["plan"] = plan

        db.log_event(
            story_key,
            stage,
            "plan",
            {
                "adapter": plan.get("adapter"),
                "summary": plan.get("summary", "")[:100],
                "trajectory_score": plan.get("trajectory_score"),
            },
        )
        summary = plan.get("summary", "")
        tool_info = f"{plan.get('adapter', 'claude')} / {plan.get('model', 'sonnet')}"
        plan_text = f"✓ {summary}  [dim]({tool_info})[/]"
        emit_plan_done(story_key, plan_text)
        return state
    except GraphInterrupt:
        return state
    except Exception as e:
        log.warning(f"Planner failed: {e}")
        import traceback

        STORY_HOME.mkdir(parents=True, exist_ok=True)
        (STORY_HOME / "planner_error.log").write_text(
            f"Planner error for {story_key}:\n{traceback.format_exc()}",
            encoding="utf-8",
        )
        log_node_error(
            story_key,
            stage,
            "plan_stage_node",
            type(e).__name__,
            str(e)[:200],
            execution_count=state.get("execution_count", 0),
            recoverable=True,
            action="block_for_planner",
            file_hint=str(STORY_HOME / "planner_error.log"),
        )
        from ...orchestrator.graph import emit_plan_done

        emit_plan_done(
            story_key,
            f"⚠ 规划失败 [{type(e).__name__}]",
            ok=False,
        )
        return _block_for_planner(
            state,
            f"Planner failed: {type(e).__name__}: {e}",
        )


# -------- node: review_stage --------


def review_stage_node(state: StoryState) -> StoryState:
    """QA/评审员角色：结构化审查阶段产出。仅在 happy path 执行。

    断路器：有 last_error 时直接跳过。
    重试疲劳：超过 MAX_REVIEW_RETRIES 次直接 fail。
    """
    if _is_cancelled(state):
        return state

    # Circuit breaker
    if state.get("last_error"):
        return state

    stage = state["current_stage"]
    cfg = get_stage_config(state.get("profile", "minimal"), stage)

    # Review disabled for this stage
    if not cfg.get("review", True):
        return state

    stage_output = state.get("context", {})

    # --- Review round tracking (replaces execution_count-based fatigue) ---
    from ..gate import (
        get_review_round_count,
        increment_review_round_count,
        GateDecision,
        write_gate_report,
    )

    review_round_count = get_review_round_count(state.get("context", {}), stage)
    execution_count = state.get("execution_count", 0)

    try:
        profile_cfg = load_profile(state.get("profile", "minimal"))
        adv_cfg = AdversarialConfig.from_profile(profile_cfg)
        retry_limit = (
            adv_cfg.code_loop.max_rounds
            if adv_cfg.code_loop_enabled(stage)
            else MAX_REVIEW_RETRIES
        )
    except Exception:
        adv_cfg = AdversarialConfig()
        retry_limit = MAX_REVIEW_RETRIES

    # Gate 1: review_round_count fatigue — review actually ran retry_limit times
    if review_round_count >= retry_limit:
        gd = GateDecision(
            story_key=state["story_key"],
            stage=stage,
            gate_name="adversarial_review",
            decision="wait_confirm",
            reason_code="review_retry_limit",
            human_message=(
                f"Review retry limit reached ({retry_limit} rounds). "
                f"Manual decision required."
            ),
            executor_attempt_count=execution_count,
            review_round_count=review_round_count,
            retry_limit=retry_limit,
            evidence={"done_consumed": True},
        )
        state["last_error"] = gd.human_message
        state["review_summary"] = f"达到 review 重试上限 ({retry_limit} 轮)"
        state["_gate_decision"] = gd.to_dict()
        db.log_event(state["story_key"], stage, "gate_decision", gd.to_dict())
        db.update_story(state["story_key"], last_error=gd.human_message)
        try:
            write_gate_report(gd, state["workspace"])
        except Exception:
            pass
        if adv_cfg.code_loop_enabled(stage):
            state["_pre_routed_action"] = "wait_confirm"
        return state

    # Gate 2: review never ran but stale executor attempts exceed threshold
    if review_round_count == 0 and execution_count >= retry_limit:
        gd = GateDecision(
            story_key=state["story_key"],
            stage=stage,
            gate_name="adversarial_review",
            decision="wait_confirm",
            reason_code="review_not_run_due_to_stale_executor_attempt_count",
            human_message=(
                f"Review did not run because executor attempts ({execution_count}) "
                f"exceeded guard threshold ({retry_limit}). Manual decision required."
            ),
            executor_attempt_count=execution_count,
            review_round_count=0,
            retry_limit=retry_limit,
            evidence={"done_consumed": True},
        )
        state["last_error"] = gd.human_message
        state["review_summary"] = (
            f"执行次数 ({execution_count}) 超过守护阈值但 review 尚未运行"
        )
        state["_gate_decision"] = gd.to_dict()
        db.log_event(state["story_key"], stage, "gate_decision", gd.to_dict())
        db.update_story(state["story_key"], last_error=gd.human_message)
        try:
            write_gate_report(gd, state["workspace"])
        except Exception:
            pass
        if adv_cfg.code_loop_enabled(stage):
            state["_pre_routed_action"] = "wait_confirm"
        return state

    # --- Adversarial code review loop ---
    try:
        if adv_cfg.code_loop_enabled(stage):
            from ..evaluator_loop import run_code_review_loop

            # Increment review_round_count BEFORE running the review loop
            new_round = increment_review_round_count(state["context"], stage)
            db.update_context(
                state["story_key"], f"review_round_count_{stage}", str(new_round)
            )

            loop_result = run_code_review_loop(state, adv_cfg, stage_output)

            # Handle non-pass decisions from the loop itself
            if loop_result.decision == "fail":
                gd = GateDecision(
                    story_key=state["story_key"],
                    stage=stage,
                    gate_name="adversarial_review",
                    decision="wait_confirm",
                    reason_code="review_unavailable",
                    human_message=(
                        loop_result.reason or "Adversarial code review failed"
                    ),
                    executor_attempt_count=execution_count,
                    review_round_count=new_round,
                    retry_limit=retry_limit,
                    reviewer={
                        "kind": "llm_api",
                        "adapter": "",
                        "model": adv_cfg.resolve_reviewer_model("code"),
                    },
                )
                state["last_error"] = gd.human_message
                state["review_summary"] = (
                    f"Adversarial review failed: {loop_result.reason}"
                )
                state["_gate_decision"] = gd.to_dict()
                db.log_event(state["story_key"], stage, "gate_decision", gd.to_dict())
                db.update_story(state["story_key"], last_error=gd.human_message)
                return state

            # No-progress detected — route to wait_confirm for human
            if loop_result.decision == "wait_confirm":
                gd = GateDecision(
                    story_key=state["story_key"],
                    stage=stage,
                    gate_name="adversarial_review",
                    decision="wait_confirm",
                    reason_code="no_progress",
                    human_message=(
                        loop_result.reason or "No progress on high findings"
                    ),
                    executor_attempt_count=execution_count,
                    review_round_count=new_round,
                    retry_limit=retry_limit,
                    reviewer={
                        "kind": "llm_api",
                        "adapter": "",
                        "model": adv_cfg.resolve_reviewer_model("code"),
                    },
                )
                state["last_error"] = gd.human_message
                state["review_summary"] = f"Adversarial review: {loop_result.reason}"
                state["_gate_decision"] = gd.to_dict()
                state["_pre_routed_action"] = "wait_confirm"
                db.log_event(state["story_key"], stage, "gate_decision", gd.to_dict())
                db.update_story(state["story_key"], last_error=gd.human_message)
                try:
                    write_gate_report(gd, state["workspace"])
                except Exception:
                    pass
                return state

            review = loop_result.final_review or {}
            workspace = state["workspace"]
            story_key = state["story_key"]

            rf = review_file(workspace, story_key, stage)
            rf.parent.mkdir(parents=True, exist_ok=True)
            issues_table = ""
            for issue in review.get("issues", []):
                issues_table += (
                    f"| {issue.get('type', '')} | {issue.get('severity', '')} "
                    f"| {issue.get('location', '')} | {issue.get('description', '')} |\n"
                )
            suggestions_list = "\n".join(
                f"- {s}" for s in review.get("suggestions", [])
            )
            no_issues_row = "| （无） | | | |\n"
            rf.write_text(
                f"# 评审: {stage} (adversarial)\n\n"
                f"## 结论: {review.get('quality', 'pass')}\n\n"
                f"## 摘要\n{review.get('summary', '')}\n\n"
                f"## 问题列表\n"
                f"| 类型 | 严重度 | 位置 | 描述 |\n"
                f"|------|--------|------|------|\n"
                f"{issues_table or no_issues_row}\n"
                f"## 建议\n{suggestions_list or '（无）'}\n\n"
                f"## 路径评分\n{review.get('trajectory_score', 'N/A')}/1.0\n\n"
                f"## 详细理由\n{review.get('reasoning', '')}",
                encoding="utf-8",
            )
            state["review_summary"] = review.get("summary", "")
            state["trajectory_score"] = review.get("trajectory_score")
            state["context"]["review_path"] = str(rf.relative_to(workspace))
            state["context"]["review_summary"] = review.get("summary", "")

            repair_path = review.get("repair_packet_path")
            if repair_path:
                state["context"]["repair_packet_path"] = (
                    str(Path(repair_path).relative_to(workspace))
                    if not Path(repair_path).is_absolute()
                    else repair_path
                )

            quality = review.get("quality", "pass")
            if quality == "revise":
                high_issues = [
                    i for i in review.get("issues", []) if i.get("severity") == "high"
                ]
                state["last_error"] = (
                    f"Review: {review.get('summary', 'needs revision')} "
                    f"({len(high_issues)} high severity issues)"
                )
            elif quality == "fail":
                state["last_error"] = f"Review failed: {review.get('summary', '')}"

            # Maintain knowledge base (same as normal path)
            _update_knowledge(workspace, story_key, stage, review, stage_output)

            # Check for learned pattern recurrence (same as normal path)
            _check_pattern_recurrence(
                workspace, story_key, stage, review.get("issues", [])
            )

            # context_updates — store index only (same as normal path)
            if review.get("context_updates"):
                for k, v in review["context_updates"].items():
                    val = str(v)
                    if len(val) > 200:
                        detail_file = (
                            context_dir(workspace, story_key) / f"{stage}_{k}.md"
                        )
                        detail_file.write_text(val, encoding="utf-8")
                        state["context"][k + "_path"] = str(
                            detail_file.relative_to(workspace)
                        )
                        state["context"][k] = val[:100] + "..."
                    else:
                        state["context"][k] = val

            db.log_event(
                story_key,
                stage,
                "review",
                {
                    "quality": quality,
                    "summary": review.get("summary", "")[:100],
                    "issues_count": len(review.get("issues", [])),
                    "trajectory_score": review.get("trajectory_score"),
                    "adversarial_loop": True,
                    "loop_rounds": loop_result.rounds,
                    "loop_decision": loop_result.decision,
                },
            )
            return state
    except Exception as e:
        log.warning(f"Adversarial code loop failed, falling back to normal review: {e}")
    # --- End adversarial code review loop ---

    try:
        # Increment review_round_count for non-adversarial review path
        new_round = increment_review_round_count(state["context"], stage)
        db.update_context(
            state["story_key"], f"review_round_count_{stage}", str(new_round)
        )

        review = planner.review_stage(state, cfg, stage_output)
        workspace = state["workspace"]
        story_key = state["story_key"]

        # Write review file
        rf = review_file(workspace, story_key, stage)
        rf.parent.mkdir(parents=True, exist_ok=True)

        issues_table = ""
        for issue in review.get("issues", []):
            issues_table += (
                f"| {issue.get('type', '')} | {issue.get('severity', '')} "
                f"| {issue.get('location', '')} | {issue.get('description', '')} |\n"
            )

        suggestions_list = "\n".join(f"- {s}" for s in review.get("suggestions", []))

        no_issues_row = "| （无） | | | |\n"
        rf.write_text(
            f"# 评审: {stage}\n\n"
            f"## 结论: {review.get('quality', 'pass')}\n\n"
            f"## 摘要\n{review.get('summary', '')}\n\n"
            f"## 问题列表\n"
            f"| 类型 | 严重度 | 位置 | 描述 |\n"
            f"|------|--------|------|------|\n"
            f"{issues_table or no_issues_row}\n"
            f"## 建议\n{suggestions_list or '（无）'}\n\n"
            f"## 路径评分\n{review.get('trajectory_score', 'N/A')}/1.0\n\n"
            f"## 详细理由\n{review.get('reasoning', '')}",
            encoding="utf-8",
        )

        # State stores index only
        state["review_summary"] = review.get("summary", "")
        state["trajectory_score"] = review.get("trajectory_score")
        state["context"]["review_path"] = str(rf.relative_to(workspace))
        state["context"]["review_summary"] = review.get("summary", "")

        # Maintain knowledge base
        _update_knowledge(workspace, story_key, stage, review, stage_output)

        # Check for learned pattern recurrence
        _check_pattern_recurrence(workspace, story_key, stage, review.get("issues", []))

        # context_updates — store index only
        if review.get("context_updates"):
            for k, v in review["context_updates"].items():
                val = str(v)
                if len(val) > 200:
                    detail_file = context_dir(workspace, story_key) / f"{stage}_{k}.md"
                    detail_file.write_text(val, encoding="utf-8")
                    state["context"][k + "_path"] = str(
                        detail_file.relative_to(workspace)
                    )
                    state["context"][k] = val[:100] + "..."
                else:
                    state["context"][k] = val

        quality = review.get("quality", "pass")
        if quality == "revise":
            high_issues = [
                i for i in review.get("issues", []) if i.get("severity") == "high"
            ]
            state["last_error"] = (
                f"Review: {review.get('summary', 'needs revision')} "
                f"({len(high_issues)} high severity issues)"
            )
        elif quality == "fail":
            state["last_error"] = f"Review failed: {review.get('summary', '')}"

        db.log_event(
            story_key,
            stage,
            "review",
            {
                "quality": quality,
                "summary": review.get("summary", "")[:100],
                "issues_count": len(review.get("issues", [])),
                "trajectory_score": review.get("trajectory_score"),
            },
        )
        return state
    except Exception as e:
        log.warning(f"Reviewer failed, skipping review: {e}")
        log_node_error(
            state["story_key"],
            stage,
            "review_stage_node",
            type(e).__name__,
            str(e)[:200],
            execution_count=state.get("execution_count", 0),
            recoverable=True,
            action="skip_review",
        )

    return state


def execute_stage_node(state: StoryState) -> dict:
    """Dispatch stage execution via Tool abstraction."""
    if _is_cancelled(state):
        return state

    import tempfile as _tf

    stage = state["current_stage"]
    workspace = state["workspace"]
    key = state["story_key"]
    profile = state.get("profile", "minimal")

    # Clear stale CLI exit marker from previous attempts
    _exit_marker = Path(_tf.gettempdir()) / f"story-exit-{key}"
    _exit_marker.unlink(missing_ok=True)

    # Fast path: if done file already exists, skip execution entirely
    done_file = stage_done_file(workspace, key, stage)
    if done_file.exists():
        # poll_completion_node is the single consumer of .story/done files.
        # Leaving the file in place lets the graph continue to poll_completion
        # and advance the stage instead of waiting for a file we already deleted.
        log.info(f"Stage {stage} done file found, skipping execution")
        return state

    cfg = get_stage_config(profile, stage)

    # Read adapter/provider/model from plan (fallback to profile config)
    plan = state.get("plan") or {}
    adapter_name = plan.get("adapter") or cfg.get(
        "cli", load_profile(profile).get("cli", "claude")
    )
    provider = plan.get("provider") or state.get("context", {}).get(
        "_provider", cfg.get("provider", "deepseek")
    )
    model = plan.get("model") or cfg.get("model", "sonnet")

    # Render prompt. With planner task packet, static stage prompt is fallback only:
    # inject planner packet + fixed stage contract, not two full task documents.
    prompt = ""
    prompt_meta = {}
    plan_path = state.get("context", {}).get("plan_path")
    if plan_path:
        plan_file = Path(workspace) / plan_path
        if plan_file.exists():
            plan_content = plan_file.read_text(encoding="utf-8")
            prompt, prompt_meta = _build_plan_executor_prompt(
                stage, state, plan_content
            )
    if not prompt:
        prompt, prompt_meta = _render_prompt(stage, state)

    # Build tool args
    stage_cfg = get_stage_config(profile, stage)
    tool_args = {
        "adapter": adapter_name,
        "provider": provider,
        "model": model,
        "prompt": prompt,
        "skill": stage_cfg.get("skill", ""),
    }

    # Observability: log prompt context
    try:
        import hashlib

        prompt_meta["prompt_sha256"] = hashlib.sha256(
            prompt.encode("utf-8")
        ).hexdigest()
        # Stable quality context hash — only packet + checklist, not full prompt
        quality_text = prompt_meta.get("quality_packet_text", "") + prompt_meta.get(
            "checklist_text", ""
        )
        prompt_meta["quality_context_sha256"] = hashlib.sha256(
            quality_text.encode("utf-8")
        ).hexdigest()
        log_prompt_context(state, prompt_meta)
    except Exception:
        log_node_error(
            state["story_key"],
            stage,
            "execute_stage_node",
            "ObservabilityError",
            "Failed to log prompt_context",
            execution_count=state.get("execution_count", 0),
            recoverable=True,
            action="continue_without_quality_context",
        )

    # Dispatch to tool
    tool_name = plan.get("tool", "stage_tool")
    from ..tools import get_tool

    tool = get_tool(tool_name)
    return tool.execute(state, tool_args)


# -------- node: poll_completion --------


def poll_completion_node(state: StoryState) -> StoryState:
    """Wait for CC to write .story/done/{story_key}/{stage}.json.

    Uses interrupt() to yield the worker thread when file not ready.
    Watchdog resumes via graph.invoke(None, config).
    """
    if _is_cancelled(state):
        return state

    import time as _time

    key = state["story_key"]
    stage = state["current_stage"]
    workspace = state["workspace"]
    done_file = stage_done_file(workspace, key, stage)

    # Check for done file first — if output exists, the stage succeeded
    # regardless of whether the session is still alive.
    if done_file.exists():
        data = None
        parse_exc = None
        for attempt in range(5):
            try:
                data = robust_json_parse(done_file)
                break
            except PermissionError:
                _time.sleep(0.5)
            except Exception as exc:
                parse_exc = exc
                break
        if data is None:
            # Snapshot malformed file before reporting error
            malformed = malformed_done_file(workspace, key, stage)
            malformed.parent.mkdir(parents=True, exist_ok=True)
            try:
                import shutil as _shutil

                _shutil.move(str(done_file), str(malformed))
            except Exception:
                pass
            state["last_error"] = f"Failed to parse done file: {parse_exc}"
            log_node_error(
                key,
                stage,
                "poll_completion_node",
                "JSONParseError",
                f"Failed to parse {done_file}: {parse_exc}",
                execution_count=state.get("execution_count", 0),
                recoverable=True,
                action="set_last_error",
                file_hint=str(done_file),
            )
            return state
        # Snapshot successfully parsed done before consuming
        snapshot = done_snapshot_file(workspace, key, stage)
        snapshot.parent.mkdir(parents=True, exist_ok=True)
        try:
            snapshot.write_text(done_file.read_text(encoding="utf-8"), encoding="utf-8")
        except Exception:
            pass
        try:
            done_file.unlink()
        except PermissionError:
            pass
        ttyd.clear_launch_state(key)
        # Stage-scope synthetic flag so it doesn't pollute other stages
        is_synthetic = data.pop("synthetic", None)
        state["context"].update(data)
        if is_synthetic:
            state["context"][f"_synthetic_{stage}"] = True
        cfg = get_stage_config(state.get("profile", "minimal"), stage)
        for field in cfg.get("expected_outputs", []):
            if field in data:
                db.update_context(key, field, str(data[field]))
        return state

    # No done file yet — check if the session exited (only for mplex-launched sessions)
    session = ttyd.session_name(key)
    if key in ttyd._mplex_launched and not ttyd.session_alive(session):
        state["last_error"] = ""
        log_node_error(
            key,
            stage,
            "poll_completion_node",
            "SessionClosed",
            f"Session {session} closed, no done file yet — keeping story active",
            execution_count=state.get("execution_count", 0),
            recoverable=True,
            action="keep_active",
        )
        return state

    # Check CLI exit marker (written when user/aI exits the terminal)
    import tempfile as _tf

    exit_marker = Path(_tf.gettempdir()) / f"story-exit-{key}"
    if exit_marker.exists():
        try:
            ec = exit_marker.read_text().strip()
            exit_marker.unlink(missing_ok=True)
        except Exception:
            ec = "?"
        state["last_error"] = ""
        log_node_error(
            key,
            stage,
            "poll_completion_node",
            "TerminalClosed",
            f"Terminal closed (exit: {ec}), stage {stage} still in progress",
            execution_count=state.get("execution_count", 0),
            recoverable=True,
            action="keep_active",
        )
        return state

    # Session alive but no done file — yield and wait
    from ...orchestrator.graph import _tui_app

    if _tui_app is None:
        # Headless mode — subprocess already completed in _launch_in_session.
        # If done_file still missing, the CLI failed to produce it.
        state["last_error"] = "Headless CLI completed without producing .done file"
        log_node_error(
            key,
            stage,
            "poll_completion_node",
            "HeadlessNoDoneFile",
            f"Headless CLI finished but no .story/done for stage {stage}",
            execution_count=state.get("execution_count", 0),
            recoverable=True,
            action="set_last_error",
        )
        return state

    interrupt({"reason": "waiting_for_done_file", "stage": stage})
    return state


# -------- node: router --------


def router_node(state: StoryState) -> StoryState:
    """Compute routing decision and store in state. Does NOT return a string."""
    if _is_cancelled(state):
        return state

    key = state["story_key"]
    stage = state["current_stage"]
    cfg = get_stage_config(state.get("profile", "minimal"), stage)

    # Preserve pre-routed decision (e.g. adversarial retry exhaustion → wait_confirm)
    pre_routed = state.pop("_pre_routed_action", None)
    if pre_routed:
        state["_next_action"] = pre_routed
        log_route_decision(state, pre_routed, "pre_routed", router_mode="adversarial")
        return state

    # 1. Retry fatigue — review hit max retries
    if state.get("review_summary") and "达到重试上限" in (
        state.get("review_summary") or ""
    ):
        db.log_event(
            key, stage, "router", {"action": "fail", "reason": "retry_fatigue"}
        )
        state["_next_action"] = "fail"
        log_route_decision(state, "fail", "retry_fatigue", router_mode="review")
        return state

    # 2. Low trajectory score — hard kill
    score = state.get("trajectory_score")
    if score is not None and score < 0.3:
        db.log_event(
            key,
            stage,
            "router",
            {
                "action": "fail",
                "reason": "low_trajectory_score",
                "score": score,
            },
        )
        state["_next_action"] = "fail"
        log_route_decision(state, "fail", "low_trajectory_score", router_mode="rule")
        return state

    # 3. Happy path — no error
    if not state.get("last_error"):
        if cfg.get("confirm"):
            state["_next_action"] = "wait_confirm"
            log_route_decision(state, "wait_confirm", "happy_path", router_mode="rule")
        else:
            state["_next_action"] = "advance"
            log_route_decision(state, "advance", "happy_path", router_mode="rule")
        return state

    # 4. Review-driven retry — have both error and review context
    if str(state.get("last_error", "")).startswith("Missing expected outputs:"):
        db.log_event(
            key,
            stage,
            "router",
            {"action": "fail", "reason": "missing_expected_outputs"},
        )
        state["_next_action"] = "fail"
        log_route_decision(
            state, "fail", "missing_expected_outputs", router_mode="rule"
        )
        return state

    if state.get("last_error") and state.get("review_summary"):
        from ..gate import get_review_round_count

        count = get_review_round_count(state.get("context", {}), stage)
        # Use adversarial max_rounds if configured, else global limit
        try:
            profile_cfg = load_profile(state.get("profile", "minimal"))
            adv_cfg = AdversarialConfig.from_profile(profile_cfg)
            retry_limit = (
                adv_cfg.code_loop.max_rounds
                if adv_cfg.code_loop_enabled(stage)
                else MAX_REVIEW_RETRIES
            )
        except Exception:
            retry_limit = MAX_REVIEW_RETRIES
        if count < retry_limit:
            db.log_event(
                key,
                stage,
                "router",
                {
                    "action": "retry",
                    "reason": "review_driven",
                    "attempt": count,
                },
            )
            state["_next_action"] = "retry"
            log_route_decision(state, "retry", "review_driven", router_mode="review")
            return state
        db.log_event(
            key,
            stage,
            "router",
            {
                "action": "fail",
                "reason": "review_retry_exhausted",
                "attempts": count,
            },
        )
        state["_next_action"] = "fail"
        log_route_decision(
            state, "fail", "review_retry_exhausted", router_mode="review"
        )
        return state

    # 4.5 Hard limit: execution_count exceeded stage max_retries
    max_retries = cfg.get("max_retries", 3)
    if state.get("execution_count", 0) >= max_retries:
        db.log_event(
            key,
            stage,
            "router",
            {
                "action": "wait_confirm",
                "reason": "execution_count_exceeded",
                "count": state["execution_count"],
            },
        )
        state["_next_action"] = "wait_confirm"
        state["last_error"] = (
            f"执行次数 ({state['execution_count']}) 已达上限 ({max_retries})，等待人工决定"
        )
        log_route_decision(
            state, "wait_confirm", "execution_count_exceeded", router_mode="rule"
        )
        return state

    # 5. Unhappy path — no review, fall through to LLM router
    try:
        decision = llm_router.route(state, cfg)
        state["_router_decision"] = decision
    except Exception as e:
        log_route_decision(
            state,
            "fail",
            f"llm_exception:{type(e).__name__}",
            router_mode="llm_fallback",
            extra={"llm_error": str(e)[:200]},
        )
        # Also write old event for backward compat
        db.log_event(
            key,
            stage,
            "router",
            {"action": "fail", "reason": f"llm_exception:{type(e).__name__}"},
        )
        state["_next_action"] = "fail"
        return state

    action = decision.get("action", "fail")
    if action == "retry":
        if decision.get("provider_override"):
            state["context"]["_provider"] = decision["provider_override"]
        state["_next_action"] = "retry"
    elif action == "skip":
        state["_next_action"] = "skip"
    else:
        state["_next_action"] = "fail"
    log_route_decision(state, state["_next_action"], "llm_router", router_mode="llm")
    return state


# -------- node: advance --------


def advance_node(state: StoryState) -> StoryState:
    """Validate expected_outputs, then advance to next stage."""
    if _is_cancelled(state):
        return state

    key = state["story_key"]
    stage = state["current_stage"]

    # Shared validation: expected_outputs + artifact gates (swebench finalize, etc.)
    from ..validation import validate_stage_outputs

    profile_data = load_profile(state.get("profile", "minimal"))
    result = validate_stage_outputs(state, profile_config=profile_data)
    if not result.ok:
        state["last_error"] = result.reason
        # Structured validation failure event for observability
        db.log_event(
            key,
            stage,
            "validation_failure",
            {
                "validator": result.details.get("validator", "unknown"),
                "reason": result.reason,
                "details": result.details,
                "execution_count": state.get("execution_count", 0),
            },
        )
        log_node_error(
            key,
            stage,
            "advance_node",
            "ValidationFailed",
            result.reason,
            execution_count=state.get("execution_count", 0),
            recoverable=True,
            action="set_last_error",
        )
        return state

    # DoD gate: block on open high findings
    try:
        from ..quality import check_dod

        dod = check_dod(key, stage)
        # Enrich dod with counts for observability
        try:
            open_high = db.get_open_findings(key, min_severity="high")
            dod["open_high_count"] = len(open_high)
            verifications = db.get_recent_quality_events(
                key, ["verification_result"], limit=1
            )
            dod["verification_present"] = len(verifications) > 0
        except Exception:
            dod.setdefault("open_high_count", 0)
            dod.setdefault("verification_present", False)
        log_dod_check(state, dod)
        if not dod["passed"]:
            state["last_error"] = f"DoD gate failed: {dod['blocking']}"
            return state
    except Exception as e:
        log_node_error(
            key,
            stage,
            "advance_node",
            type(e).__name__,
            str(e)[:200],
            execution_count=state.get("execution_count", 0),
            recoverable=False,
            action="do_not_silently_pass",
        )
        state["last_error"] = f"DoD check failed: {e}"
        return state

    next_stage = resolve_next_stage(state)
    if not next_stage:
        db.update_story(key, current_stage=stage, status="completed")
        db.log_stage(key, stage, "complete", "All stages done")
        state["status"] = "completed"
        notify("Story Lifecycle", f"Story {key}: 全部阶段完成")

        # Sync status to external source (P1)
        story = db.get_story(key)
        if story:
            source_type = story.get("source_type")
            source_id = story.get("source_id")
            if source_type and source_id:
                try:
                    from ...sources import get_source

                    source = get_source(source_type)
                    if source:
                        source.sync_status(source_id, "completed")
                except Exception as e:
                    log.warning(f"Failed to sync status to {source_type}: {e}")
        return state

    db.log_stage(key, stage, "complete", f"Advanced to {next_stage}")
    db.update_story(key, current_stage=next_stage, status="active")
    notify("Story Lifecycle", f"Story {key}: {stage} 完成，进入 {next_stage}")

    state["current_stage"] = next_stage
    state["status"] = "active"
    state["execution_count"] = 0
    state.pop("_next_action", None)
    state.pop("_pre_routed_action", None)

    # Clean up PRD task file after design stage completes
    if stage == "design":
        workspace = state.get("workspace", "") or str(Path.cwd())
        prd_task_file = Path(workspace) / ".story" / f"prd-task-{key}.json"
        try:
            prd_task_file.unlink(missing_ok=True)
        except Exception:
            pass

    return state


# -------- node: retry --------


def retry_node(state: StoryState) -> StoryState:
    """Prepare for retry. Clear error, keep count."""
    if _is_cancelled(state):
        return state

    state["last_error"] = None
    state.pop("_next_action", None)
    state.pop("_pre_routed_action", None)
    db.log_stage(
        state["story_key"],
        state["current_stage"],
        "retry",
        f"Retry {state.get('execution_count', 0) + 1}",
    )
    return state


# -------- node: skip --------


def skip_node(state: StoryState) -> StoryState:
    """Skip current stage. Auto-fill expected_outputs with SKIPPED."""
    if _is_cancelled(state):
        return state

    cfg = get_stage_config(state.get("profile", "minimal"), state["current_stage"])
    for field in cfg.get("expected_outputs", []):
        if field not in state.get("context", {}):
            state["context"][field] = "SKIPPED"
            db.update_context(state["story_key"], field, "SKIPPED")

    db.log_stage(state["story_key"], state["current_stage"], "skip", "Skipped by user")
    db.update_story(state["story_key"], status="active")
    state["status"] = "active"
    state["last_error"] = None
    return state


# -------- node: fail --------


def fail_node(state: StoryState) -> StoryState:
    """Mark story as blocked."""
    if _is_cancelled(state):
        return state

    key = state["story_key"]
    stage = state["current_stage"]
    error = state.get("last_error") or "Unknown error"
    db.update_story(key, status="blocked", last_error=error)
    db.log_stage(key, stage, "fail", error)
    state["status"] = "blocked"
    notify("Story Lifecycle", f"Story {key}: {stage} 失败 — {error[:80]}")
    return state


# -------- node: wait_confirm --------


def wait_confirm_node(state: StoryState) -> StoryState:
    """Pause for human confirmation. Writes gate report and persists GateDecision."""
    if _is_cancelled(state):
        return state

    key = state["story_key"]
    stage = state["current_stage"]

    from ..gate import GateDecision, write_gate_report, gate_decision_from_state

    # Retrieve or build gate decision
    gd_dict = state.pop("_gate_decision", None)
    if gd_dict:
        gd = GateDecision.from_dict(gd_dict)
    else:
        gd = gate_decision_from_state(state)

    # Persist gate decision
    db.update_story(key, status="paused", last_error=gd.human_message)
    db.log_event(key, stage, "gate_decision", gd.to_dict())

    # Compact gate_result table entry
    try:
        db.record_gate_result(
            key,
            stage,
            gd.gate_name,
            gd.decision,
            json.dumps({"reason_code": gd.reason_code, "decision_id": gd.decision_id}),
        )
    except Exception:
        pass

    db.log_stage(key, stage, "pause", gd.human_message)

    # Write gate report
    report_rel = ""
    try:
        rp = write_gate_report(gd, state["workspace"])
        report_rel = str(Path(rp).relative_to(Path(state["workspace"])))
    except Exception:
        pass

    # Update context_json for TUI visibility
    db.update_context(key, "last_gate_decision_id", gd.decision_id)
    db.update_context(key, "last_gate_decision", gd.decision)
    db.update_context(key, "last_gate_reason_code", gd.reason_code)
    if report_rel:
        db.update_context(key, "last_gate_report_path", report_rel)

    # Sync in-memory context
    state["context"]["last_gate_decision_id"] = gd.decision_id
    state["context"]["last_gate_decision"] = gd.decision
    state["context"]["last_gate_reason_code"] = gd.reason_code
    state["context"]["last_gate_report_path"] = report_rel

    state["status"] = "paused"
    state["last_error"] = gd.human_message

    # Yield thread — Watchdog or user action will resume
    interrupt({"reason": "waiting_for_confirmation", "stage": stage})

    # Resumed — check if user set status back to active
    s = db.get_story(key)
    if s and s["status"] == "active":
        state["status"] = "active"
        state["execution_count"] = 0

    return state


# -------- prompt rendering --------
