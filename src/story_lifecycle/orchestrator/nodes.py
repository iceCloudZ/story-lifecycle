"""LangGraph node implementations — plan, execute, poll, review, advance, skip, retry, fail."""

import json
import logging
import re
from pathlib import Path
from typing import TypedDict, Optional

import yaml

from langgraph.errors import GraphInterrupt
from langgraph.types import interrupt

from ..db import models as db
from ..terminal import ttyd
from . import planner
from . import router as llm_router
from .notify import send as notify
from .observability import (
    log_node_error,
    log_route_decision,
    log_prompt_context,
    log_dod_check,
)
from .evaluator_loop import AdversarialConfig

log = logging.getLogger("story-lifecycle.nodes")

TIMEOUT_SECONDS = 30 * 60  # 30 minutes per stage
POLL_INTERVAL = 15  # seconds between poll checks
STORY_HOME = Path.home() / ".story-lifecycle"
MAX_REVIEW_RETRIES = 3


class StoryState(TypedDict, total=False):
    story_key: str
    title: str
    workspace: str
    profile: str
    current_stage: str
    status: str
    complexity: str
    context: dict
    execution_count: int
    last_error: Optional[str]
    stage_start_time: float

    # Smart Orchestrator fields
    plan_summary: Optional[str]
    review_summary: Optional[str]
    trajectory_score: Optional[float]
    plan: Optional[dict]
    _next_action: Optional[str]
    _pending_sub_keys: Optional[list]
    _router_decision: Optional[dict]


# -------- stage config --------


def load_profile(profile_name: str) -> dict:
    """Load a profile YAML. Searches: project .story/ → STORY_HOME → built-in."""
    for base in [
        Path.cwd() / ".story",
        STORY_HOME,
        Path(__file__).parent.parent.parent.parent,  # package root (story-lifecycle/)
    ]:
        path = base / "profiles" / f"{profile_name}.yaml"
        if path.exists():
            return yaml.safe_load(path.read_text(encoding="utf-8"))
    raise FileNotFoundError(f"Profile not found: {profile_name}")


def get_stage_config(profile_name: str, stage_name: str) -> dict:
    profile = load_profile(profile_name)
    stages = profile.get("stages", {})
    return stages.get(stage_name, {})


def resolve_next_stage(state: StoryState) -> Optional[str]:
    """Determine next stage from profile config + complexity."""
    cfg = get_stage_config(state.get("profile", "minimal"), state["current_stage"])
    next_map = cfg.get("next_default", {})

    if isinstance(next_map, list):
        return next_map[0] if next_map else None
    if isinstance(next_map, dict):
        complexity = state.get("complexity", "M")
        candidates = next_map.get(complexity, next_map.get("default", []))
        return candidates[0] if candidates else None
    return None


# -------- robust JSON parsing --------


# -------- robust JSON parsing --------


def _extract_json_object(text: str) -> str | None:
    """Extract the first complete JSON object using bracket counting."""
    depth = 0
    start = None
    for i, ch in enumerate(text):
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and start is not None:
                return text[start : i + 1]
    return None


def robust_json_parse(filepath: Path) -> dict:
    """Parse .done JSON with tolerance for markdown wrapping."""
    raw = filepath.read_text(encoding="utf-8")

    # Strategy 1: direct parse
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass

    # Strategy 2: bracket-counting extraction (handles arbitrary nesting)
    extracted = _extract_json_object(raw)
    if extracted:
        try:
            return json.loads(extracted)
        except json.JSONDecodeError:
            pass

    # Strategy 3: try extracting between ```json fences
    m = re.search(r"```json\s*\n(.*?)\n\s*```", raw, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            pass

    raise ValueError(f"Cannot parse JSON from {filepath}: {raw[:200]}")


# -------- routing functions --------


def route_after_plan(state: StoryState) -> str:
    """Conditional edge after plan_stage: skip, execute, or end."""
    if state.get("status") == "skipping":
        return "skip_stage"
    if state.get("status") == "waiting_subtasks":
        return "__end__"
    return "execute_stage"


def route_after_poll(state: StoryState) -> str:
    """Conditional edge after poll_completion: review or router (if error)."""
    if state.get("last_error"):
        return "router"
    return "review_stage"


# -------- node: plan_stage --------


def plan_stage_node(state: StoryState) -> StoryState:
    """架构师/PM 角色：规划当前阶段。无 LLM 时退化为默认 plan。

    不调用其他节点。skip 通过 conditional edge 路由。
    """
    stage = state["current_stage"]
    profile = state.get("profile", "minimal")
    cfg = get_stage_config(profile, stage)
    workspace = state["workspace"]
    story_key = state["story_key"]

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
        if adv_cfg.plan_loop_enabled(stage) and planner.is_available():
            from ..orchestrator.graph import emit_plan_done
            from .evaluator_loop import run_plan_loop

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
                plan_file = (
                    Path(workspace) / ".story-context" / story_key / f"plan_{stage}.md"
                )
                plan_file.parent.mkdir(parents=True, exist_ok=True)
                review_path = state.get("context", {}).get("review_path")
                review_section = ""
                if review_path:
                    rf = Path(workspace) / review_path
                    if rf.exists():
                        review_section = (
                            f"\n## 前序 Review 建议\n"
                            f"请先处理以下问题：\n{rf.read_text(encoding='utf-8')}"
                        )
                plan_file.write_text(
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
                state["context"]["plan_path"] = str(plan_file.relative_to(workspace))
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

            # Non-pass decisions: block and wait for human
            if loop_result.decision in ("no_progress", "max_rounds", "fail"):
                from ..orchestrator.graph import emit_plan_done

                reason = f"Plan loop {loop_result.decision}: {loop_result.reason}"
                state["plan_summary"] = reason
                state["last_error"] = reason
                emit_plan_done(story_key, f"⚠ {reason}", ok=False)
                return state
    except Exception as e:
        log.warning(f"Adversarial plan loop failed, falling back to normal: {e}")
    # --- End adversarial plan loop ---

    if planner.is_available():
        try:
            from ..orchestrator.graph import emit_plan_done

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

            # Planner decided to split into sub-stories
            if plan.get("split") and plan.get("subtasks"):
                return _delegate_subtasks(state, plan)

            # Write plan task file
            plan_file = (
                Path(workspace) / ".story-context" / story_key / f"plan_{stage}.md"
            )
            plan_file.parent.mkdir(parents=True, exist_ok=True)

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

            plan_file.write_text(
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

            # State stores index only
            state["plan_summary"] = plan.get("summary", "")
            state["trajectory_score"] = plan.get("trajectory_score")
            state["context"]["plan_path"] = str(plan_file.relative_to(workspace))
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
            # Build human-readable plan result
            tool_info = (
                f"{plan.get('adapter', 'claude')} / {plan.get('model', 'sonnet')}"
            )
            plan_text = f"✓ {summary}  [dim]({tool_info})[/]"
            emit_plan_done(story_key, plan_text)
            return state
        except GraphInterrupt:
            return state
        except Exception as e:
            log.warning(f"Planner failed, falling back: {e}")
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
                action="fallback_to_default_plan",
                file_hint=str(STORY_HOME / "planner_error.log"),
            )
            emit_plan_done(
                story_key,
                f"⚠ 规划降级 [{type(e).__name__}] 使用默认配置",
                ok=False,
            )

    # Fallback: generate plan from profile config
    profile_cfg = load_profile(profile)
    state["plan"] = {
        "adapter": cfg.get("cli", profile_cfg.get("cli", "claude")),
        "provider": state.get("context", {}).get(
            "_provider", cfg.get("provider", "deepseek")
        ),
        "model": cfg.get("model", "sonnet"),
        "skip": False,
        "extra_instructions": "",
        "summary": "Fallback: using profile config",
        "reasoning": "Fallback: using profile config",
        "trajectory_score": None,
    }
    state["plan_summary"] = "Fallback: using profile config"
    from ..orchestrator.graph import emit_plan_done

    emit_plan_done(story_key, "使用默认配置启动", ok=True)
    return state


def _delegate_subtasks(state: StoryState, plan: dict) -> StoryState:
    """Split a parent story into sub-stories. Stores sub_keys in state for
    the graph runner to launch. Uses interrupt() to pause the parent.
    """
    import shutil

    parent_key = state["story_key"]
    workspace = state["workspace"]
    profile = state.get("profile", "minimal")
    stage = state["current_stage"]
    subtasks = plan["subtasks"]

    active_sub_keys = []
    for i, sub in enumerate(subtasks):
        sub_key = f"{parent_key}-{sub['key_suffix']}"
        has_deps = bool(sub.get("depends_on"))
        sub_status = "blocked" if has_deps else "active"

        db.upsert_story(
            sub_key,
            title=sub.get("title", ""),
            workspace=workspace,
            profile=profile,
            current_stage=stage,
            status=sub_status,
            parent_key=parent_key,
            subtask_index=i,
        )

        # Copy parent knowledge to sub-story (Windows-safe, no symlinks)
        parent_knowledge = Path(workspace) / ".story-knowledge" / parent_key
        sub_knowledge = Path(workspace) / ".story-knowledge" / sub_key
        if parent_knowledge.exists():
            sub_knowledge.mkdir(parents=True, exist_ok=True)
            for f in parent_knowledge.glob("*.md"):
                shutil.copy2(str(f), str(sub_knowledge / f.name))

        # Write per-subtask plan file
        plan_dir = Path(workspace) / ".story-context" / sub_key
        plan_dir.mkdir(parents=True, exist_ok=True)
        plan_file = plan_dir / f"plan_{stage}.md"
        plan_file.write_text(
            f"# 子任务: {sub.get('title', '')}\n\n"
            f"## 所属 Story\n{parent_key} 的子任务 ({i + 1}/{len(subtasks)})\n\n"
            f"## 执行指令\n{sub.get('summary', '')}\n\n"
            f"## 约束\n这是子任务，只负责本模块的实现，不要修改其他模块。\n",
            encoding="utf-8",
        )

        if sub_status == "active":
            active_sub_keys.append(sub_key)

        db.log_event(
            parent_key,
            stage,
            "delegate",
            {
                "sub_key": sub_key,
                "title": sub.get("title", ""),
                "depends_on": sub.get("depends_on", []),
                "status": sub_status,
            },
        )

    # Store keys for graph runner to launch (no circular import)
    state["_pending_sub_keys"] = active_sub_keys
    state["status"] = "waiting_subtasks"
    state["plan_summary"] = f"拆分为 {len(subtasks)} 个子任务"
    db.update_story(parent_key, status="waiting_subtasks")
    db.log_event(
        parent_key,
        stage,
        "split",
        {
            "subtask_count": len(subtasks),
            "sub_keys": [f"{parent_key}-{s['key_suffix']}" for s in subtasks],
        },
    )

    # Pause parent via interrupt — resume_story will wake it up
    interrupt({"reason": "waiting_for_subtasks", "sub_count": len(subtasks)})
    return state


# -------- node: review_stage --------


def review_stage_node(state: StoryState) -> StoryState:
    """QA/评审员角色：结构化审查阶段产出。仅在 happy path 执行。

    断路器：有 last_error 时直接跳过。
    重试疲劳：超过 MAX_REVIEW_RETRIES 次直接 fail。
    """
    # Circuit breaker
    if state.get("last_error"):
        return state

    stage = state["current_stage"]
    cfg = get_stage_config(state.get("profile", "minimal"), stage)

    # Review disabled for this stage
    if not cfg.get("review", True):
        return state

    stage_output = state.get("context", {})

    # Retry fatigue
    execution_count = state.get("execution_count", 0)
    if execution_count >= MAX_REVIEW_RETRIES:
        state["last_error"] = f"Review retry limit reached ({MAX_REVIEW_RETRIES} times)"
        state["review_summary"] = f"达到重试上限 ({MAX_REVIEW_RETRIES} 次)"
        db.log_event(
            state["story_key"],
            stage,
            "review",
            {"quality": "forced_fail", "retries": execution_count},
        )
        return state

    # --- Adversarial code review loop ---
    try:
        profile_cfg = load_profile(state.get("profile", "minimal"))
        adv_cfg = AdversarialConfig.from_profile(profile_cfg)
        if adv_cfg.code_loop_enabled(stage) and planner.is_available():
            from .evaluator_loop import run_code_review_loop

            loop_result = run_code_review_loop(state, adv_cfg, stage_output)

            # Handle non-pass decisions from the loop itself
            if loop_result.decision == "fail":
                state["last_error"] = (
                    loop_result.reason or "Adversarial code review failed"
                )
                state["review_summary"] = (
                    f"Adversarial review failed: {loop_result.reason}"
                )
                return state

            review = loop_result.final_review or {}
            workspace = state["workspace"]
            story_key = state["story_key"]

            review_file = (
                Path(workspace) / ".story-context" / story_key / f"review_{stage}.md"
            )
            review_file.parent.mkdir(parents=True, exist_ok=True)
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
            review_file.write_text(
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
            state["context"]["review_path"] = str(review_file.relative_to(workspace))
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

    if planner.is_available():
        try:
            review = planner.review_stage(state, cfg, stage_output)
            workspace = state["workspace"]
            story_key = state["story_key"]

            # Write review file
            review_file = (
                Path(workspace) / ".story-context" / story_key / f"review_{stage}.md"
            )
            review_file.parent.mkdir(parents=True, exist_ok=True)

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
            review_file.write_text(
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
            state["context"]["review_path"] = str(review_file.relative_to(workspace))
            state["context"]["review_summary"] = review.get("summary", "")

            # Maintain knowledge base
            _update_knowledge(workspace, story_key, stage, review, stage_output)

            # Check for learned pattern recurrence
            _check_pattern_recurrence(
                workspace, story_key, stage, review.get("issues", [])
            )

            # context_updates — store index only
            if review.get("context_updates"):
                for k, v in review["context_updates"].items():
                    val = str(v)
                    if len(val) > 200:
                        detail_file = (
                            Path(workspace)
                            / ".story-context"
                            / story_key
                            / f"{stage}_{k}.md"
                        )
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


def _check_pattern_recurrence(
    workspace: str, story_key: str, stage: str, issues: list[dict]
):
    """Check if review issues match any active learned patterns (recurrence detection)."""
    if not issues:
        return

    try:
        patterns = db.get_active_learned_patterns(limit=20)
    except Exception:
        return

    if not patterns:
        return

    recurrences = []
    mode = "rule_fallback"

    try:
        from .semantic import match_pattern_recurrence

        for issue in issues:
            result = match_pattern_recurrence(issue, patterns)
            mode = result.get("mode", "rule_fallback")
            for m in result["data"].get("matches", []):
                pid = m["pattern_id"]
                pattern_obj = next((p for p in patterns if p["id"] == pid), None)
                if pattern_obj:
                    recurrences.append(
                        {
                            "pattern_id": pid,
                            "pattern": pattern_obj.get("pattern", ""),
                            "confidence": m.get("confidence", "low"),
                            "reasoning": m.get("reasoning", ""),
                            "issue": issue,
                        }
                    )
                else:
                    recurrences.append(
                        {
                            "pattern_id": pid,
                            "pattern": "",
                            "confidence": m.get("confidence", "low"),
                            "reasoning": m.get("reasoning", ""),
                            "issue": issue,
                        }
                    )
    except Exception:
        # Fallback to original keyword matching
        for issue in issues:
            desc = issue.get("description", "")
            cat = issue.get("category", "")
            issue_text = f"{cat} {desc}".lower()
            for p in patterns:
                if _match_pattern(issue_text, p.get("pattern", ""), p.get("rule", "")):
                    recurrences.append(
                        {
                            "pattern_id": p["id"],
                            "pattern": p.get("pattern", ""),
                            "confidence": "low",
                            "reasoning": "keyword fallback",
                            "issue": issue,
                        }
                    )
                    break

    if recurrences:
        db.log_event(
            story_key,
            stage,
            "pattern_recurrence",
            {
                "mode": mode,
                "recurrences": recurrences,
                "count": len(recurrences),
            },
        )


def _match_pattern(issue_text: str, pattern_name: str, rule: str) -> bool:
    """Simple keyword/substring matching against pattern name and rule."""
    keywords = (pattern_name + " " + rule).lower().split()
    # Require at least 2 keyword matches to avoid false positives
    matches = sum(1 for kw in keywords if len(kw) >= 2 and kw in issue_text)
    return matches >= 2


def _update_knowledge(
    workspace: str, story_key: str, stage: str, review: dict, stage_output: dict
):
    """Reviewer maintains Story-level knowledge base."""
    knowledge_dir = Path(workspace) / ".story-knowledge" / story_key
    knowledge_dir.mkdir(parents=True, exist_ok=True)

    # design.md on design stage pass
    if stage == "design" and review.get("quality") == "pass":
        design_file = knowledge_dir / "design.md"
        design_file.write_text(
            f"# 设计要点: {story_key}\n\n"
            f"## 需求概述\n{stage_output.get('summary', '')}\n\n"
            f"## 复杂度\n{stage_output.get('complexity', 'M')}\n\n"
            f"## 技术约束\n{stage_output.get('constraints', '无特殊约束')}",
            encoding="utf-8",
        )

    # Append decisions
    decisions_file = knowledge_dir / "decisions.md"
    if not decisions_file.exists():
        decisions_file.write_text(f"# 决策记录: {story_key}\n", encoding="utf-8")

    with open(decisions_file, "a", encoding="utf-8") as f:
        f.write(f"\n## {stage} 阶段\n")
        f.write(f"- 结论: {review.get('summary', '')}\n")
        f.write(f"- 路径评分: {review.get('trajectory_score', 'N/A')}\n")
        for issue in review.get("issues", []):
            f.write(
                f"- 问题: [{issue.get('severity', '')}] "
                f"{issue.get('description', '')} @ {issue.get('location', '')}\n"
            )

    # Append constraints if found
    constraints = stage_output.get("constraints") or stage_output.get("边界条件")
    if constraints:
        constraints_file = knowledge_dir / "constraints.md"
        existing = (
            constraints_file.read_text(encoding="utf-8")
            if constraints_file.exists()
            else ""
        )
        if str(constraints) not in existing:
            with open(constraints_file, "a", encoding="utf-8") as f:
                f.write(f"\n## {stage} 阶段添加\n{constraints}\n")


# -------- node: execute_stage --------


def execute_stage_node(state: StoryState) -> StoryState:
    """Dispatch stage execution via Tool abstraction."""
    stage = state["current_stage"]
    workspace = state["workspace"]
    key = state["story_key"]
    profile = state.get("profile", "minimal")

    # Fast path: if done file already exists, skip execution entirely
    done_file = Path(workspace) / ".story-done" / key / f"{stage}.json"
    if done_file.exists():
        try:
            data = robust_json_parse(done_file)
            done_file.unlink(missing_ok=True)
            state["context"].update(data)
            cfg = get_stage_config(profile, stage)
            for field in cfg.get("expected_outputs", []):
                if field in data:
                    db.update_context(key, field, str(data[field]))
            log.info(f"Stage {stage} done file found, skipping execution")
            return state
        except Exception:
            pass

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

    # Render prompt + prepend plan file if available
    prompt, prompt_meta = _render_prompt(stage, state)
    plan_path = state.get("context", {}).get("plan_path")
    if plan_path:
        plan_file = Path(workspace) / plan_path
        if plan_file.exists():
            plan_content = plan_file.read_text(encoding="utf-8")
            prompt = f"{plan_content}\n\n---\n\n{prompt}"
            prompt_meta["has_plan_file"] = True

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
    from .tools import get_tool

    tool = get_tool(tool_name)
    return tool.execute(state, tool_args)


# -------- node: poll_completion --------


def poll_completion_node(state: StoryState) -> StoryState:
    """Wait for CC to write .story-done/{story_key}/{stage}.json.

    Uses interrupt() to yield the worker thread when file not ready.
    Watchdog resumes via graph.invoke(None, config).
    """
    import time as _time

    key = state["story_key"]
    stage = state["current_stage"]
    workspace = state["workspace"]
    done_file = Path(workspace) / ".story-done" / key / f"{stage}.json"

    # Check for done file first — if output exists, the stage succeeded
    # regardless of whether the session is still alive.
    if done_file.exists():
        data = None
        for attempt in range(5):
            try:
                data = robust_json_parse(done_file)
                break
            except PermissionError:
                _time.sleep(0.5)
            except Exception:
                break
        if data is None:
            state["last_error"] = "Failed to read .done file after retries"
            log_node_error(
                key,
                stage,
                "poll_completion_node",
                "JSONParseError",
                f"Failed to parse {done_file}",
                execution_count=state.get("execution_count", 0),
                recoverable=True,
                action="set_last_error",
                file_hint=str(done_file),
            )
            return state
        try:
            done_file.unlink()
        except PermissionError:
            pass
        ttyd.clear_launch_state(key)
        state["context"].update(data)
        cfg = get_stage_config(state.get("profile", "minimal"), stage)
        for field in cfg.get("expected_outputs", []):
            if field in data:
                db.update_context(key, field, str(data[field]))
        return state

    # No done file yet — check if the session crashed (only for mplex-launched sessions)
    session = ttyd.session_name(key)
    if key in ttyd._mplex_launched and not ttyd.session_alive(session):
        state["last_error"] = "CC process crashed (session dead)"
        log_node_error(
            key,
            stage,
            "poll_completion_node",
            "SessionDead",
            f"CC session {session} is dead, no done file found",
            execution_count=state.get("execution_count", 0),
            recoverable=True,
            action="set_last_error",
        )
        return state

    # Session alive but no done file — yield and wait
    interrupt({"reason": "waiting_for_done_file", "stage": stage})
    return state


# -------- node: router --------


def router_node(state: StoryState) -> StoryState:
    """Compute routing decision and store in state. Does NOT return a string."""
    key = state["story_key"]
    stage = state["current_stage"]
    cfg = get_stage_config(state.get("profile", "minimal"), stage)

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
        count = state.get("execution_count", 0)
        if count < MAX_REVIEW_RETRIES:
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


def route_from_router(state: StoryState) -> str:
    """Pure routing function: read _next_action from state, return edge name."""
    return state.get("_next_action", "fail")


def route_after_advance(state: StoryState) -> str:
    """After advance: route errors back through router, complete to END, otherwise continue."""
    if state.get("last_error"):
        return "router"
    if state.get("status") == "completed":
        return "__end__"
    return "plan_stage"


# -------- node: advance --------


def advance_node(state: StoryState) -> StoryState:
    """Validate expected_outputs, then advance to next stage."""
    key = state["story_key"]
    stage = state["current_stage"]
    cfg = get_stage_config(state.get("profile", "minimal"), stage)

    # Schema guard: check expected_outputs
    missing = [
        k for k in cfg.get("expected_outputs", []) if k not in state.get("context", {})
    ]
    if missing:
        state["last_error"] = f"Missing expected outputs: {missing}"
        return state  # goes back to router

    # DoD gate: block on open high findings
    try:
        from .quality import check_dod

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
                    from ..sources import get_source

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
    state["last_error"] = None
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
    """Pause for human confirmation. Yields thread via interrupt."""
    key = state["story_key"]
    db.update_story(key, status="paused")
    db.log_stage(
        key, state["current_stage"], "pause", "Waiting for manual confirmation"
    )
    state["status"] = "paused"

    # Yield thread — Watchdog or user action will resume
    interrupt({"reason": "waiting_for_confirmation", "stage": state["current_stage"]})

    # Resumed — check if user set status back to active
    s = db.get_story(key)
    if s and s["status"] == "active":
        state["status"] = "active"
        state["execution_count"] = 0

    return state


# -------- prompt rendering --------


def _derive_relevance_tags(state: StoryState, stage: str) -> list[str]:
    """Derive relevance tags from story context for pattern matching."""
    tags = [stage]
    ctx = state.get("context", {})

    # Affected modules
    modules = ctx.get("affected_modules", [])
    if isinstance(modules, list):
        tags.extend(modules)
    elif isinstance(modules, str):
        tags.append(modules)

    # Touched file paths → derive module tags
    paths = ctx.get("touched_paths", [])
    if isinstance(paths, list):
        for p in paths:
            if isinstance(p, str) and "/" in p:
                tags.append(p.split("/")[0])
            elif isinstance(p, str):
                tags.append(p)

    category = ctx.get("category")
    if category:
        tags.append(category)
    profile = state.get("profile", "")
    if profile:
        tags.append(profile)

    # Source type & sub-type from DB story record
    try:
        story = db.get_story(state["story_key"])
        if story:
            source_type = story.get("source_type")
            if source_type:
                tags.append(source_type)
            sub_type = story.get("sub_type")
            if sub_type:
                tags.append(sub_type)
    except Exception:
        pass

    return tags


def _build_prd_task_section(state: StoryState, stage: str, has_prd: bool) -> str:
    """Build AI-enhanced PRD injection section if prd-task-{story_key}.json exists."""
    if has_prd or stage != "design":
        return ""
    workspace = state.get("workspace", "") or str(Path.cwd())
    story_key = state.get("story_key", "")
    prd_task_file = Path(workspace) / ".story" / f"prd-task-{story_key}.json"
    if not prd_task_file.exists():
        return ""
    try:
        prd_task = json.loads(prd_task_file.read_text(encoding="utf-8"))
        description = prd_task.get("description", "")
        section = (
            "## AI 增强 PRD 任务\n\n"
            f"检测到 PRD 生成任务文件: `{prd_task_file}`\n\n"
            f"- **来源**: {prd_task.get('source', '未知')}\n"
            f"- **平台 ID**: {prd_task.get('source_id', '')}\n"
            f"- **标题**: {prd_task.get('title', '')}\n"
            f"- **描述**: {description}\n\n"
            "请执行以下步骤:\n"
            "1. 使用 `prd-generator` skill 生成结构化 PRD\n"
            "2. 将生成的 PRD 保存到合适位置（如 `prd/` 目录）\n"
            "3. 在 `.story-done/` 目录写入完成标记\n"
        )
        return section
    except Exception:
        return ""


def _render_prompt(stage: str, state: StoryState) -> tuple[str, dict]:
    """Render a prompt for the given stage. Returns (prompt_text, metadata_dict).

    Reads built-in templates or falls back to defaults.
    """
    template_paths = [
        STORY_HOME / "prompts" / f"{stage}.md",
        Path(__file__).parent.parent.parent.parent / "prompts" / f"{stage}.md",
    ]
    template = None
    for p in template_paths:
        if p.exists():
            template = p.read_text(encoding="utf-8")
            break

    if not template:
        # Default prompt
        template = f"""执行阶段: {stage}
Story: {state["story_key"]}
标题: {state["title"]}

完成后将结果写入项目根目录下的 `.story-done/{state["story_key"]}/{stage}.json`。
文件必须只包含纯 JSON，不要用 markdown 代码块包裹。"""

    # Variable substitution
    ctx = state.get("context", {})

    # Sub-story context injection (type-aware)
    parent_key = None
    current_story = db.get_story(state["story_key"])
    if current_story:
        parent_key = current_story.get("parent_key")
    if parent_key:
        parent_story = db.get_story(parent_key)
        parent_title = parent_story.get("title", "") if parent_story else ""
        sub_desc = ctx.get("sub_description", "")
        sub_type = current_story.get("sub_type") or ""

        type_emphasis = {
            "bug-fix": "修复以下问题",
            "integration": "前后端联调修改",
            "refinement": "需求补充/调整",
            "redo": "重做",
        }
        emphasis = type_emphasis.get(sub_type, "子任务")

        context_hints = ""
        if sub_type == "bug-fix":
            review_path = ctx.get("review_path")
            if review_path:
                context_hints += (
                    f"\n- 上次评审: {review_path}\n  请关注评审中提到的问题。"
                )
        elif sub_type == "integration":
            spec_path = ctx.get("spec_path")
            if spec_path:
                context_hints += (
                    f"\n- 接口文档: {spec_path}\n  请参考设计文档中的接口定义。"
                )
        elif sub_type == "refinement":
            spec_path = ctx.get("spec_path")
            if spec_path:
                context_hints += (
                    f"\n- 现有设计文档: {spec_path}\n  在此基础上进行补充和调整。"
                )
        elif sub_type == "redo":
            review_path = ctx.get("review_path")
            review_summary = ctx.get("review_summary", "")
            if review_path:
                context_hints += f"\n- 被否决的方案评审: {review_path}"
            if review_summary:
                context_hints += f"\n- 评审摘要: {review_summary}"
            context_hints += "\n  请推翻旧方案，重新设计和实现。"

        sub_header = (
            f"## 子任务上下文\n\n"
            f"- **父故事**: {parent_key} — {parent_title}\n"
            f"- **类型**: {sub_type} — {emphasis}\n"
            f"- **任务描述**: {sub_desc}\n"
            f"{context_hints}\n"
        )
        template = sub_header + template

    # Quality Packet injection
    quality_section = ""
    checklist = ""
    quality_packet_injected = False
    quality_checklist_injected = False
    open_findings_count = 0
    learned_patterns_count = 0
    relevance_tags: list[str] = []
    try:
        from .quality import build_quality_packet, build_quality_checklist

        relevance_tags = _derive_relevance_tags(state, stage)
        quality_packet = build_quality_packet(
            state["story_key"], stage, relevant_tags=relevance_tags
        )
        empty_marker = (
            f"Quality Packet for {state['story_key']}\n\nOpen Findings: none\n"
        )
        if quality_packet.strip() != empty_marker.strip():
            quality_section = f"## Quality Packet\n\n{quality_packet}"
            quality_packet_injected = True
        checklist = build_quality_checklist(state["story_key"], stage)
        if checklist.strip():
            quality_checklist_injected = True
        # Count findings and patterns from the packet for metadata
        try:
            from ..db import models as _qdb

            findings = _qdb.get_open_findings(state["story_key"])
            open_findings_count = len(findings)
            patterns = _qdb.find_relevant_patterns(relevance_tags, limit=5)
            learned_patterns_count = len(patterns)
        except Exception:
            pass
    except Exception:
        pass

    # Repair packet injection
    repair_section = ""
    repair_packet_path = ctx.get("repair_packet_path")
    if repair_packet_path:
        rp_file = Path(state["workspace"]) / repair_packet_path
        if rp_file.exists():
            repair_content = rp_file.read_text(encoding="utf-8")
            repair_section = f"## Repair Packet（修复上下文）\n\n{repair_content}"

    has_prd = bool(ctx.get("prd_path"))

    # Get stage skill from profile
    profile_name = state.get("profile", "minimal")
    stage_cfg = get_stage_config(profile_name, stage)
    skill = stage_cfg.get("skill", "")

    vars_map = {
        "{story_key}": state["story_key"],
        "{title}": state.get("title", ""),
        "{prd_path}": ctx.get("prd_path", ""),
        "{prd_path_section}": (
            f"- PRD 文件: {ctx['prd_path']}\n  请读取该文件了解需求详情。"
            if has_prd
            else ""
        ),
        "{no_prd_section}": (
            ""
            if has_prd
            else "**没有提供 PRD 文件。**\n"
            "1. 先扫描项目目录，查找已有的需求文档（`docs/`、`prd/`、`requirements/` 等），找到则直接使用\n"
            "2. 如果没找到，向用户询问需求详情（TAPD/Jira 链接、文字描述、或其他文档）"
        ),
        # AI-enhanced PRD injection
        "{prd_task_section}": _build_prd_task_section(state, stage, has_prd),
        "{requirement_source}": (
            "阅读 PRD 文件" if has_prd else "查找项目已有文档或与用户对话，获取需求详情"
        ),
        "{spec_path_section}": (
            f"- Spec 路径: {ctx['spec_path']}" if ctx.get("spec_path") else ""
        ),
        "{skill}": skill,
        "{skill_instruction}": (
            f"请先执行 skill: `{skill}` 来进行结构化分析，然后基于分析结果完成本阶段任务。"
            if skill
            else ""
        ),
        "{quality_packet_section}": quality_section,
        "{quality_checklist}": checklist,
        "{repair_packet_section}": repair_section,
    }
    _had_repair_placeholder = "{repair_packet_section}" in template
    for key, value in vars_map.items():
        template = template.replace(key, value)

    # Append repair packet directly if template had no placeholder
    if repair_section and not _had_repair_placeholder:
        template = f"{template}\n\n{repair_section}"

    metadata = {
        "quality_packet_injected": quality_packet_injected,
        "quality_checklist_injected": quality_checklist_injected,
        "quality_packet_text": quality_section,
        "checklist_text": checklist,
        "open_findings_count": open_findings_count,
        "learned_patterns_count": learned_patterns_count,
        "relevance_tags": relevance_tags,
        "has_prd": has_prd,
        "has_plan_file": False,  # set by caller when plan file prepended
    }
    return template, metadata
