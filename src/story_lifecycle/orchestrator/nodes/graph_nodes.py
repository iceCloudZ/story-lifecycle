"""LangGraph node implementations — 5-node architecture.

Nodes: plan_stage, execute_and_wait, review_stage, router, advance.
retry/skip/fail/wait_confirm are handled inside router_node.
"""

import json
import logging
import tempfile as _tf
from pathlib import Path

from langgraph.errors import GraphInterrupt

from ...db import models as db
from ...terminal import ttyd
from ...terminal.pty import get_pty
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
from .stage_resolver import _is_cancelled, _block_for_planner, resolve_next_stage
from .errors import NodeError
from .subtask_delegate import _delegate_subtasks
from .knowledge import _check_pattern_recurrence, _update_knowledge
from .json_helpers import robust_json_parse
from .prompt_renderer import (
    _build_plan_executor_prompt,
    _render_prompt,
)


def _apply_gate_decision(
    state: StoryState,
    gd,  # GateDecision
    stage: str,
    *,
    review_summary: str = "",
    write_report: bool = True,
    pre_route: bool = False,
) -> StoryState:
    """Apply a GateDecision to state, DB, and optionally write report.

    Consolidates 4 duplicate patterns in review_stage_node.
    """
    from ..gate import write_gate_report

    state["last_error"] = gd.human_message
    if review_summary:
        state["review_summary"] = review_summary
    state["_gate_decision"] = gd.to_dict()
    db.log_event(state["story_key"], stage, "gate_decision", gd.to_dict())
    db.update_story(state["story_key"], last_error=gd.human_message)
    if write_report:
        try:
            write_gate_report(gd, state["workspace"])
        except Exception:
            pass
    if pre_route:
        state["_pre_routed_action"] = "wait_confirm"
    return state


def _sync_story_source(state: StoryState, key: str, stage: str, ctx: dict) -> None:
    """Sync context to external story source.

    Consolidates 2 duplicate patterns in advance_node.
    """
    story_rec = db.get_story(key)
    if not story_rec:
        return
    source_type = story_rec.get("source_type")
    source_id = story_rec.get("source_id")
    if not source_type or not source_id:
        return
    try:
        from ...sources import get_source

        source = get_source(source_type)
        if source:
            # Read done data if available
            workspace = state.get("workspace", "")
            done_path = (
                Path(workspace) / ".story-done" / f"{stage}.json" if workspace else None
            )
            if done_path and done_path.exists():
                try:
                    raw = done_path.read_text(encoding="utf-8")
                    done_data = robust_json_parse(raw)
                    if done_data:
                        ctx["done_data"] = done_data
                except Exception:
                    pass
            _try_sync_context(source, source_id, stage, ctx)
    except Exception as e:
        log.warning(f"Failed to sync context to {source_type}: {e}")


def _rp(state: StoryState) -> dict:
    """Get resolved profile from state, or load it as fallback."""
    rp = state.get("_resolved_profile")
    if rp is None:
        from .profile_loader import resolve_profile

        rp = resolve_profile(state.get("profile", "minimal")).to_dict()
        state["_resolved_profile"] = rp
    return rp


def _stage_cfg(state: StoryState, stage: str) -> dict:
    """Get resolved stage config from state's resolved profile."""
    rp = _rp(state)
    return rp.get("stages", {}).get(stage, {})


def _write_plan_task_file(
    state: StoryState,
    plan: dict,
    stage: str,
    workspace: str,
    cfg: dict,
    *,
    adversarial: bool = False,
    loop_rounds: int = 0,
    loop_decision: str = "",
) -> StoryState:
    """Write plan task file to disk and update state.

    Shared by adversarial loop path and normal planner fallback.
    """
    story_key = state["story_key"]
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

    # Build output hint (adversarial path includes expected_outputs signal)
    output_hint = ""
    if adversarial:
        expected_outputs = cfg.get("expected_outputs", [])
        done_path = f".story/done/{story_key}/{stage}.json"
        output_example = {k: "..." for k in expected_outputs}
        if expected_outputs:
            import json as _json

            output_hint = (
                f"\n## 完成信号\n"
                f"完成后必须写入 `{done_path}`，纯 JSON（不要 markdown 代码块）：\n"
                f"```json\n{_json.dumps(output_example, ensure_ascii=False, indent=2)}\n```\n"
            )

    # Build constraints section (adversarial path includes extra constraints)
    constraints_section = ""
    if adversarial:
        constraints_section = (
            "## 约束\n"
            "- 所有表名、字段名、类名必须以代码或数据库定义为准；未找到时标记为假设，不得写成已存在事实\n"
            "- 如需澄清的需求点，写入 open_questions 字段，不要阻塞当前产出\n\n"
        )

    pf.write_text(
        f"# 任务书: {stage}\n\n"
        f"## 执行指令\n{plan.get('extra_instructions', '')}\n"
        f"{review_section}\n"
        f"{constraints_section}"
        f"## 配置\n"
        f"- Adapter: {plan.get('adapter', 'claude')}\n"
        f"- Provider: {plan.get('provider', 'deepseek')}\n"
        f"- Model: {plan.get('model', 'sonnet')}\n\n"
        f"{output_hint}"
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

    event_data = {
        "adapter": plan.get("adapter"),
        "summary": plan.get("summary", "")[:100],
        "trajectory_score": plan.get("trajectory_score"),
    }
    if adversarial:
        event_data.update(
            {
                "adversarial_loop": True,
                "loop_rounds": loop_rounds,
                "loop_decision": loop_decision,
            }
        )
    db.log_event(story_key, stage, "plan", event_data)
    return state


def _try_sync_context(source, source_id: str, stage: str, context: dict):
    try:
        from ...sources.github_source import GithubSource

        if isinstance(source, GithubSource):
            source.sync_context(source_id, stage, context)
    except Exception as e:
        log.warning(f"sync_context failed for {source_id}: {e}")


def _ws_notify(state: StoryState, status: str = ""):
    try:
        from ..api import notify_story_update_sync

        notify_story_update_sync(
            state["story_key"],
            status=status or state.get("status", ""),
            stage=state.get("current_stage", ""),
        )
    except Exception:
        pass


log = logging.getLogger("story-lifecycle.nodes")


# ============================================================
# Node 1: plan_stage
# ============================================================


def plan_stage_node(state: StoryState) -> StoryState:
    if _is_cancelled(state):
        return state

    stage = state["current_stage"]
    cfg = _stage_cfg(state, stage)
    workspace = state["workspace"]
    story_key = state["story_key"]

    active_execution = state.get("context", {}).get("_active_execution")
    current_done = stage_done_file(workspace, story_key, stage)
    if current_done.exists() or (
        isinstance(active_execution, dict) and active_execution.get("stage") == stage
    ):
        return state

    # Guard: execution_count exceeded max_retries
    max_retries = cfg.get("max_retries", 3)
    if state.get("execution_count", 0) >= max_retries:
        state["_pre_routed_action"] = "wait_confirm"
        state["last_error"] = (
            f"执行次数 ({state['execution_count']}) 已达上限 ({max_retries})，等待人工决定"
        )
        return state

    # Clear stale routing state
    state.pop("_next_action", None)
    state.pop("_pre_routed_action", None)

    # Trigger Condenser
    try:
        compressed_path = planner.compress_context(workspace, story_key, stage)
        if compressed_path:
            state["context"]["knowledge_path"] = compressed_path
            db.log_event(story_key, stage, "condense", {"output": compressed_path})
    except Exception as e:
        log.warning(f"Condenser failed: {e}")

    # --- Adversarial plan loop ---
    try:
        profile_cfg = _rp(state)
        adv_cfg = AdversarialConfig.from_profile(profile_cfg)
        if adv_cfg.plan_loop_enabled(stage):
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
                return state

            # Pass with plan
            if loop_result.decision == "pass" and loop_result.final_plan:
                plan = loop_result.final_plan
                return _write_plan_task_file(
                    state,
                    plan,
                    stage,
                    workspace,
                    cfg,
                    adversarial=True,
                    loop_rounds=loop_result.rounds,
                    loop_decision=loop_result.decision,
                )

            # Non-pass decisions: route to wait_confirm
            if loop_result.decision in ("no_progress", "max_rounds", "fail"):
                reason = f"Plan loop {loop_result.decision}: {loop_result.reason}"
                state["plan_summary"] = reason
                state["last_error"] = reason
                state["_pre_routed_action"] = "wait_confirm"
                return state
    except Exception as e:
        log.warning(f"Adversarial plan loop failed, falling back to normal: {e}")
    # --- End adversarial plan loop ---

    try:
        adapters = ["claude"]
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

        # Split into sub-stories
        if plan.get("split") and plan.get("subtasks"):
            _delegate_subtasks(state, plan)
            return state

        # Write plan task file
        return _write_plan_task_file(state, plan, stage, workspace, cfg)
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
        NodeError(
            "plan_stage_node",
            stage,
            f"Planner failed: {type(e).__name__}: {e}",
            error_type=type(e).__name__,
            action="block_for_planner",
            meta={"file_hint": str(STORY_HOME / "planner_error.log")},
        ).apply(state)
        return _block_for_planner(
            state,
            f"Planner failed: {type(e).__name__}: {e}",
        )


# ============================================================
# Node 2: execute_and_wait (merges execute_stage + poll_completion)
# ============================================================


def execute_and_wait_node(state: StoryState) -> dict:
    if _is_cancelled(state):
        return state

    stage = state["current_stage"]
    workspace = state["workspace"]
    key = state["story_key"]

    done_file = stage_done_file(workspace, key, stage)

    # --- Phase 1: Consume existing done file (idempotent) ---
    if done_file.exists():
        data = _consume_done_file(done_file, state, key, stage)
        if data is not None:
            state["context"].update(data)
            _sync_done_outputs(state, key, stage, data)
            return state
        # Parse failed — error already logged, don't proceed to dispatch
        return state

    # --- Phase 2: Dispatch tool execution ---
    cfg = _stage_cfg(state, stage)
    plan = state.get("plan") or {}
    adapter_name = plan.get("adapter") or cfg.get("cli", "claude")
    provider = plan.get("provider") or state.get("context", {}).get(
        "_provider", cfg.get("provider", "deepseek")
    )
    model = plan.get("model") or cfg.get("model", "sonnet")
    execution_mode = cfg.get("execution_mode", "interactive_pty")

    active_execution = state.get("context", {}).get("_active_execution")
    existing_pty = get_pty(key)
    if (
        execution_mode == "interactive_pty"
        and isinstance(active_execution, dict)
        and active_execution.get("stage") == stage
        and existing_pty is not None
        and existing_pty.alive
        and existing_pty.purpose == "agent"
    ):
        state["_execution_mode"] = execution_mode
        state["_waiting_for_agent"] = True
        return state

    prompt = ""
    prompt_meta = {}
    plan_path = state.get("context", {}).get("plan_path")
    if plan_path:
        plan_file_path = Path(workspace) / plan_path
        if plan_file_path.exists():
            plan_content = plan_file_path.read_text(encoding="utf-8")
            prompt, prompt_meta = _build_plan_executor_prompt(
                stage, state, plan_content
            )
    if not prompt:
        prompt, prompt_meta = _render_prompt(stage, state)

    stage_cfg = _stage_cfg(state, stage)
    tool_args = {
        "adapter": adapter_name,
        "provider": provider,
        "model": model,
        "execution_mode": execution_mode,
        "prompt": prompt,
        "skill": stage_cfg.get("skill", ""),
    }

    # Observability
    try:
        import hashlib

        prompt_meta["prompt_sha256"] = hashlib.sha256(
            prompt.encode("utf-8")
        ).hexdigest()
        quality_text = prompt_meta.get("quality_packet_text", "") + prompt_meta.get(
            "checklist_text", ""
        )
        prompt_meta["quality_context_sha256"] = hashlib.sha256(
            quality_text.encode("utf-8")
        ).hexdigest()
        log_prompt_context(state, prompt_meta)
    except Exception:
        log_node_error(
            key,
            stage,
            "execute_and_wait_node",
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
    _ws_notify(state, "active")
    tool.execute(state, tool_args)

    # --- Phase 3: Poll for completion ---
    return _poll_done_file(state, done_file, key, stage, workspace)


def _consume_done_file(
    done_file: Path, state: StoryState, key: str, stage: str
) -> dict | None:
    """Parse and consume a done file. Returns parsed data or None on error."""
    import time as _time

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
        malformed = malformed_done_file(Path(state["workspace"]), key, stage)
        malformed.parent.mkdir(parents=True, exist_ok=True)
        try:
            import shutil as _shutil

            _shutil.move(str(done_file), str(malformed))
        except Exception:
            pass
        NodeError(
            "execute_and_wait_node",
            stage,
            f"Failed to parse done file: {parse_exc}",
            error_type="JSONParseError",
            action="set_last_error",
            meta={"file_hint": str(done_file)},
        ).apply(state)
        return None

    # Snapshot before consuming
    snapshot = done_snapshot_file(Path(state["workspace"]), key, stage)
    snapshot.parent.mkdir(parents=True, exist_ok=True)
    try:
        snapshot.write_text(done_file.read_text(encoding="utf-8"), encoding="utf-8")
    except Exception:
        pass
    try:
        done_file.unlink()
    except PermissionError:
        pass
    state.get("context", {}).pop("_active_execution", None)
    db.update_story(
        key,
        context_json=json.dumps(state.get("context", {}), ensure_ascii=False),
    )

    is_synthetic = data.pop("synthetic", None)
    if is_synthetic:
        data[f"_synthetic_{stage}"] = True
    return data


def _sync_done_outputs(state: StoryState, key: str, stage: str, data: dict):
    """Write done file outputs to DB context."""
    cfg = _stage_cfg(state, stage)
    for field in cfg.get("expected_outputs", []):
        if field in data:
            db.update_context(key, field, str(data[field]))


def _poll_done_file(
    state: StoryState, done_file: Path, key: str, stage: str, workspace: str
) -> dict:
    """Poll for done file, check session/exit status."""
    # Check done file
    if done_file.exists():
        data = _consume_done_file(done_file, state, key, stage)
        if data is not None:
            state["context"].update(data)
            _sync_done_outputs(state, key, stage, data)
        return state

    if state.get("_waiting_for_agent"):
        return state

    # Check session exit (mplex-launched only)
    session = ttyd.session_name(key)
    if key in ttyd._mplex_launched and not ttyd.session_alive(session):
        NodeError(
            "execute_and_wait_node",
            stage,
            f"Session {session} closed, no done file yet — keeping story active",
            error_type="SessionClosed",
            action="keep_active",
        ).apply(state)
        return state

    # Check CLI exit marker
    exit_marker = Path(_tf.gettempdir()) / f"story-exit-{key}"
    if exit_marker.exists():
        try:
            ec = exit_marker.read_text().strip()
            exit_marker.unlink(missing_ok=True)
        except Exception:
            ec = "?"
        NodeError(
            "execute_and_wait_node",
            stage,
            f"Terminal closed (exit: {ec}), stage {stage} still in progress",
            error_type="TerminalClosed",
            action="keep_active",
        ).apply(state)
        return state

    # Headless mode — CLI finished without producing done file
    NodeError(
        "execute_and_wait_node",
        stage,
        "Headless CLI completed without producing .done file",
        error_type="HeadlessNoDoneFile",
        action="set_last_error",
    ).apply(state)
    return state


# ============================================================
# Node 3: review_stage
# ============================================================


def review_stage_node(state: StoryState) -> StoryState:
    if _is_cancelled(state):
        return state

    # Circuit breaker
    if state.get("last_error"):
        return state

    stage = state["current_stage"]
    cfg = _stage_cfg(state, stage)

    if not cfg.get("review", True):
        return state

    stage_output = state.get("context", {})

    from ..gate import (
        get_review_round_count,
        increment_review_round_count,
        GateDecision,
    )

    review_round_count = get_review_round_count(state.get("context", {}), stage)
    execution_count = state.get("execution_count", 0)

    try:
        profile_cfg = _rp(state)
        adv_cfg = AdversarialConfig.from_profile(profile_cfg)
        retry_limit = (
            adv_cfg.code_loop.max_rounds
            if adv_cfg.code_loop_enabled(stage)
            else MAX_REVIEW_RETRIES
        )
    except Exception:
        adv_cfg = AdversarialConfig()
        retry_limit = MAX_REVIEW_RETRIES

    # Gate 1: review round fatigue
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
        return _apply_gate_decision(
            state,
            gd,
            stage,
            review_summary=f"达到 review 重试上限 ({retry_limit} 轮)",
            pre_route=adv_cfg.code_loop_enabled(stage),
        )

    # Gate 2: stale executor attempts
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
        return _apply_gate_decision(
            state,
            gd,
            stage,
            review_summary=(
                f"执行次数 ({execution_count}) 超过守护阈值但 review 尚未运行"
            ),
            pre_route=adv_cfg.code_loop_enabled(stage),
        )

    # --- Adversarial code review loop ---
    try:
        if adv_cfg.code_loop_enabled(stage):
            from ..evaluator_loop import run_code_review_loop

            new_round = increment_review_round_count(state["context"], stage)
            db.update_context(
                state["story_key"], f"review_round_count_{stage}", str(new_round)
            )

            loop_result = run_code_review_loop(state, adv_cfg, stage_output)

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
                return _apply_gate_decision(
                    state,
                    gd,
                    stage,
                    review_summary=f"Adversarial review failed: {loop_result.reason}",
                    write_report=False,
                )

            if loop_result.decision in ("wait_confirm", "no_progress"):
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
                return _apply_gate_decision(
                    state,
                    gd,
                    stage,
                    review_summary=f"Adversarial review: {loop_result.reason}",
                    pre_route=True,
                )

            review = loop_result.final_review or {}
            _apply_review(
                state,
                review,
                stage,
                "adversarial",
                loop_result.rounds,
                loop_result.decision,
            )
            return state
    except Exception as e:
        log.warning(f"Adversarial code loop failed, falling back to normal review: {e}")
    # --- End adversarial code review loop ---

    try:
        new_round = increment_review_round_count(state["context"], stage)
        db.update_context(
            state["story_key"], f"review_round_count_{stage}", str(new_round)
        )

        review = planner.review_stage(state, cfg, stage_output)
        _apply_review(state, review, stage)
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


def _apply_review(
    state: StoryState,
    review: dict,
    stage: str,
    mode: str = "normal",
    loop_rounds: int = 0,
    loop_decision: str = "",
):
    """Write review results to state and disk."""
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
    suggestions_list = "\n".join(f"- {s}" for s in review.get("suggestions", []))
    no_issues_row = "| （无） | | | |\n"
    label = f" ({mode})" if mode != "normal" else ""
    rf.write_text(
        f"# 评审: {stage}{label}\n\n"
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

    # Sync review summary to source
    try:
        _story_rec = db.get_story(story_key)
        if _story_rec:
            _src_type = _story_rec.get("source_type")
            _src_id = _story_rec.get("source_id")
            if _src_type and _src_id:
                from ...sources import get_source

                _src = get_source(_src_type)
                if _src:
                    _try_sync_context(
                        _src,
                        _src_id,
                        stage,
                        {"review_summary": review.get("summary", "")},
                    )
    except Exception as e:
        log.warning(f"Failed to sync review to source: {e}")

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

    _update_knowledge(workspace, story_key, stage, review, state.get("context", {}))
    _check_pattern_recurrence(workspace, story_key, stage, review.get("issues", []))

    if review.get("context_updates"):
        for k, v in review["context_updates"].items():
            val = str(v)
            if len(val) > 200:
                detail_file = context_dir(workspace, story_key) / f"{stage}_{k}.md"
                detail_file.write_text(val, encoding="utf-8")
                state["context"][k + "_path"] = str(detail_file.relative_to(workspace))
                state["context"][k] = val[:100] + "..."
            else:
                state["context"][k] = val

    event_data = {
        "quality": quality,
        "summary": review.get("summary", "")[:100],
        "issues_count": len(review.get("issues", [])),
        "trajectory_score": review.get("trajectory_score"),
    }
    if mode == "adversarial":
        event_data.update(
            {
                "adversarial_loop": True,
                "loop_rounds": loop_rounds,
                "loop_decision": loop_decision,
            }
        )
    db.log_event(story_key, stage, "review", event_data)


# ============================================================
# Node 4: router (merges retry/skip/fail/wait_confirm)
# ============================================================


def router_node(state: StoryState) -> dict:
    """Route to next action: advance, retry (→plan_stage), skip (→advance),
    fail (→END), or wait_confirm (→plan_stage)."""
    if _is_cancelled(state):
        return {"_next_action": "__end__"}

    key = state["story_key"]
    stage = state["current_stage"]
    cfg = _stage_cfg(state, stage)

    # Pre-routed decision (adversarial exhaustion, gate decisions)
    pre_routed = state.pop("_pre_routed_action", None)
    if pre_routed:
        log_route_decision(state, pre_routed, "pre_routed", router_mode="adversarial")
        return _execute_action(state, pre_routed, cfg)

    # 1. Retry fatigue — review hit max retries
    if state.get("review_summary") and "达到重试上限" in (
        state.get("review_summary") or ""
    ):
        db.log_event(
            key, stage, "router", {"action": "fail", "reason": "retry_fatigue"}
        )
        log_route_decision(state, "fail", "retry_fatigue", router_mode="review")
        return _execute_action(state, "fail", cfg)

    # 2. Low trajectory score — hard kill
    score = state.get("trajectory_score")
    if score is not None and score < 0.3:
        db.log_event(
            key,
            stage,
            "router",
            {"action": "fail", "reason": "low_trajectory_score", "score": score},
        )
        log_route_decision(state, "fail", "low_trajectory_score", router_mode="rule")
        return _execute_action(state, "fail", cfg)

    # 3. Happy path — no error
    if not state.get("last_error"):
        if cfg.get("confirm"):
            log_route_decision(state, "wait_confirm", "happy_path", router_mode="rule")
            return _execute_action(state, "wait_confirm", cfg)
        log_route_decision(state, "advance", "happy_path", router_mode="rule")
        return _execute_action(state, "advance", cfg)

    # 4. Missing expected outputs → fail
    if str(state.get("last_error", "")).startswith("Missing expected outputs:"):
        db.log_event(
            key,
            stage,
            "router",
            {"action": "fail", "reason": "missing_expected_outputs"},
        )
        log_route_decision(
            state, "fail", "missing_expected_outputs", router_mode="rule"
        )
        return _execute_action(state, "fail", cfg)

    # 5. Review-driven retry
    if state.get("last_error") and state.get("review_summary"):
        from ..gate import get_review_round_count

        count = get_review_round_count(state.get("context", {}), stage)
        try:
            profile_cfg = _rp(state)
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
                {"action": "retry", "reason": "review_driven", "attempt": count},
            )
            log_route_decision(state, "retry", "review_driven", router_mode="review")
            return _execute_action(state, "retry", cfg)

        db.log_event(
            key,
            stage,
            "router",
            {"action": "fail", "reason": "review_retry_exhausted", "attempts": count},
        )
        log_route_decision(
            state, "fail", "review_retry_exhausted", router_mode="review"
        )
        return _execute_action(state, "fail", cfg)

    # 6. Hard limit: execution_count exceeded max_retries
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
        state["last_error"] = (
            f"执行次数 ({state['execution_count']}) 已达上限 ({max_retries})，等待人工决定"
        )
        log_route_decision(
            state, "wait_confirm", "execution_count_exceeded", router_mode="rule"
        )
        return _execute_action(state, "wait_confirm", cfg)

    # 7. LLM routing for unhappy path
    try:
        decision = llm_router.route(state, cfg)
    except Exception as e:
        log_route_decision(
            state,
            "fail",
            f"llm_exception:{type(e).__name__}",
            router_mode="llm_fallback",
            extra={"llm_error": str(e)[:200]},
        )
        db.log_event(
            key,
            stage,
            "router",
            {"action": "fail", "reason": f"llm_exception:{type(e).__name__}"},
        )
        return _execute_action(state, "fail", cfg)

    action = decision.get("action", "fail")
    if action == "retry" and decision.get("provider_override"):
        state["context"]["_provider"] = decision["provider_override"]

    log_route_decision(state, action, "llm_router", router_mode="llm")
    return _execute_action(state, action, cfg)


def _execute_action(state: StoryState, action: str, cfg: dict) -> dict:
    """Execute the chosen routing action and return state update."""
    key = state["story_key"]
    stage = state["current_stage"]

    match action:
        case "advance":
            state.pop("last_error", None)
            state["_next_action"] = "advance"
            return state

        case "retry":
            state["last_error"] = None
            state.pop("_next_action", None)
            state.pop("_pre_routed_action", None)
            db.log_stage(
                key,
                stage,
                "retry",
                f"Retry {state.get('execution_count', 0) + 1}",
            )
            _ws_notify(state, "active")
            state["_next_action"] = "plan_stage"
            return state

        case "skip":
            cfg = _stage_cfg(state, stage)
            for field in cfg.get("expected_outputs", []):
                if field not in state.get("context", {}):
                    state["context"][field] = "SKIPPED"
                    db.update_context(key, field, "SKIPPED")
            db.log_stage(key, stage, "skip", "Skipped by user")
            db.update_story(key, status="active")
            state["status"] = "active"
            state.pop("last_error", None)
            state["_next_action"] = "advance"
            _ws_notify(state, "active")
            return state

        case "fail":
            error = state.get("last_error") or "Unknown error"
            db.update_story(key, status="blocked", last_error=error)
            db.log_stage(key, stage, "fail", error)
            state["status"] = "blocked"
            notify("Story Lifecycle", f"Story {key}: {stage} 失败 — {error[:80]}")
            _ws_notify(state, "blocked")
            state["_next_action"] = "__end__"
            return state

        case "wait_confirm":
            return _do_wait_confirm(state, key, stage)

        case _:
            log.warning(f"Unknown router action: {action}, defaulting to fail")
            state["_next_action"] = "__end__"
            return state


def _do_wait_confirm(state: StoryState, key: str, stage: str) -> dict:
    """Pause for human confirmation with gate report.

    Writes paused status to DB and returns — the API server or watchdog
    will detect the paused state and wait for user action to resume.
    """
    from ..gate import GateDecision, write_gate_report, gate_decision_from_state

    gd_dict = state.pop("_gate_decision", None)
    if gd_dict:
        gd = GateDecision.from_dict(gd_dict)
    else:
        gd = gate_decision_from_state(state)

    db.update_story(key, status="paused", last_error=gd.human_message)
    db.log_event(key, stage, "gate_decision", gd.to_dict())

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

    report_rel = ""
    try:
        rp = write_gate_report(gd, state["workspace"])
        report_rel = str(Path(rp).relative_to(Path(state["workspace"])))
    except Exception:
        pass

    db.update_context(key, "last_gate_decision_id", gd.decision_id)
    db.update_context(key, "last_gate_decision", gd.decision)
    db.update_context(key, "last_gate_reason_code", gd.reason_code)
    if report_rel:
        db.update_context(key, "last_gate_report_path", report_rel)

    state["context"]["last_gate_decision_id"] = gd.decision_id
    state["context"]["last_gate_decision"] = gd.decision
    state["context"]["last_gate_reason_code"] = gd.reason_code
    state["context"]["last_gate_report_path"] = report_rel

    state["status"] = "paused"
    state["last_error"] = gd.human_message
    _ws_notify(state, "paused")

    # Set next action so router sends story to plan_stage on resume
    state["_next_action"] = "plan_stage"
    return state


# ============================================================
# Node 5: advance
# ============================================================


def advance_node(state: StoryState) -> StoryState:
    """Validate expected_outputs, then advance to next stage."""
    if _is_cancelled(state):
        return state

    key = state["story_key"]
    stage = state["current_stage"]

    # Validation
    from ..validation import validate_stage_outputs

    profile_data = _rp(state)
    result = validate_stage_outputs(state, profile_config=profile_data)
    if not result.ok:
        state["last_error"] = result.reason
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
        NodeError(
            "advance_node",
            stage,
            result.reason,
            error_type="ValidationFailed",
            action="set_last_error",
        ).apply(state)
        return state

    # DoD gate
    try:
        from ..quality import check_dod

        dod = check_dod(key, stage)
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
        NodeError(
            "advance_node",
            stage,
            f"DoD check failed: {e}",
            error_type=type(e).__name__,
            recoverable=False,
            action="do_not_silently_pass",
        ).apply(state)
        return state

    next_stage = resolve_next_stage(state)
    if not next_stage:
        db.update_story(key, current_stage=stage, status="completed")
        db.log_stage(key, stage, "complete", "All stages done")
        state["status"] = "completed"
        notify("Story Lifecycle", f"Story {key}: 全部阶段完成")
        _ws_notify(state, "completed")

        # Log completion without auto-syncing to TAPD.
        # TAPD status updates are intentionally NOT done here --
        # that is a manual/scheduled operation to avoid usage exhaustion.
        db.log_event(key, stage, "lifecycle_complete", {"action": "all_stages_done"})
        _sync_story_source(state, key, stage, {})
        return state

    db.log_stage(key, stage, "complete", f"Advanced to {next_stage}")
    # Sync context to source on stage advance
    _stage_ctx = {}
    if state.get("context", {}).get("plan_summary"):
        _stage_ctx["plan_summary"] = state["context"]["plan_summary"]
    if state.get("context", {}).get("review_summary"):
        _stage_ctx["review_summary"] = state["context"]["review_summary"]
    _sync_story_source(state, key, stage, _stage_ctx)
    db.update_story(key, current_stage=next_stage, status="active")
    notify("Story Lifecycle", f"Story {key}: {stage} 完成，进入 {next_stage}")
    _ws_notify(state, "active")

    state["current_stage"] = next_stage
    state["status"] = "active"
    state["execution_count"] = 0
    state.pop("_next_action", None)
    state.pop("_pre_routed_action", None)

    # Clean up PRD task file after design stage
    if stage == "design":
        workspace = state.get("workspace", "") or str(Path.cwd())
        prd_task_file = Path(workspace) / ".story" / f"prd-task-{key}.json"
        try:
            prd_task_file.unlink(missing_ok=True)
        except Exception:
            pass

    return state
