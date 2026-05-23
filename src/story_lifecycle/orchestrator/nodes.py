"""LangGraph node implementations — plan, execute, poll, review, advance, skip, retry, fail."""

import json
import logging
import re
from pathlib import Path
from typing import TypedDict, Optional

import yaml

from langgraph.types import interrupt

from ..db import models as db
from ..terminal import ttyd
from . import planner
from . import router as llm_router
from .notify import send as notify

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
    """Conditional edge after plan_stage: skip or execute."""
    if state.get("status") == "skipping":
        return "skip_stage"
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
        except Exception as e:
            log.warning(f"Planner failed, falling back: {e}")
            import traceback

            (STORY_HOME / "planner_error.log").write_text(
                f"Planner error for {story_key}:\n{traceback.format_exc()}",
                encoding="utf-8",
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

    # No expected_outputs → skip review
    if not stage_output or not cfg.get("expected_outputs"):
        return state

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

    return state


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
    profile = state.get("profile", "minimal")

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
    prompt = _render_prompt(stage, state)
    plan_path = state.get("context", {}).get("plan_path")
    if plan_path:
        plan_file = Path(workspace) / plan_path
        if plan_file.exists():
            plan_content = plan_file.read_text(encoding="utf-8")
            prompt = f"{plan_content}\n\n---\n\n{prompt}"

    # Build tool args
    stage_cfg = get_stage_config(profile, stage)
    tool_args = {
        "adapter": adapter_name,
        "provider": provider,
        "model": model,
        "prompt": prompt,
        "skill": stage_cfg.get("skill", ""),
    }

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
            return state
        try:
            done_file.unlink()
        except PermissionError:
            pass
        state["context"].update(data)
        cfg = get_stage_config(state.get("profile", "minimal"), stage)
        for field in cfg.get("expected_outputs", []):
            if field in data:
                db.update_context(key, field, str(data[field]))
        return state

    # No done file yet — check if the session crashed
    session = ttyd.session_name(key)
    if ttyd._MPLEX and not ttyd.session_alive(session):
        state["last_error"] = "CC process crashed (session dead)"
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
        return state

    # 3. Happy path — no error
    if not state.get("last_error"):
        if cfg.get("confirm"):
            state["_next_action"] = "wait_confirm"
        else:
            state["_next_action"] = "advance"
        return state

    # 4. Review-driven retry — have both error and review context
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
        return state

    # 5. Unhappy path — no review, fall through to LLM router
    decision = llm_router.route(state, cfg)
    state["_router_decision"] = decision

    action = decision.get("action", "fail")
    if action == "retry":
        if decision.get("provider_override"):
            state["context"]["_provider"] = decision["provider_override"]
        state["_next_action"] = "retry"
    elif action == "skip":
        state["_next_action"] = "skip"
    else:
        state["_next_action"] = "fail"
    return state


def route_from_router(state: StoryState) -> str:
    """Pure routing function: read _next_action from state, return edge name."""
    return state.get("_next_action", "fail")


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

    next_stage = resolve_next_stage(state)
    if not next_stage:
        db.update_story(key, current_stage=stage, status="completed")
        db.log_stage(key, stage, "complete", "All stages done")
        state["status"] = "completed"
        notify("Story Lifecycle", f"Story {key}: 全部阶段完成")
        return state

    db.log_stage(key, stage, "complete", f"Advanced to {next_stage}")
    db.update_story(key, current_stage=next_stage, status="active")
    notify("Story Lifecycle", f"Story {key}: {stage} 完成，进入 {next_stage}")

    state["current_stage"] = next_stage
    state["status"] = "active"
    state["execution_count"] = 0
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
    error = state.get("last_error", "Unknown error")
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


def _render_prompt(stage: str, state: StoryState) -> str:
    """Render a prompt for the given stage. Reads built-in templates or falls back to defaults."""
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

    # Sub-story context injection
    parent_key = None
    current_story = db.get_story(state["story_key"])
    if current_story:
        parent_key = current_story.get("parent_key")
    if parent_key:
        parent_story = db.get_story(parent_key)
        parent_title = parent_story.get("title", "") if parent_story else ""
        sub_desc = ctx.get("sub_description", "")
        sub_type = current_story.get("sub_type") or ""

        sub_header = (
            f"## 子任务上下文\n\n"
            f"- **父故事**: {parent_key} — {parent_title}\n"
            f"- **类型**: {sub_type}\n"
            f"- **任务描述**: {sub_desc}\n\n"
        )
        template = sub_header + template

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
    }
    for key, value in vars_map.items():
        template = template.replace(key, value)

    return template
