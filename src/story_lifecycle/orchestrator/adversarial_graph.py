"""LangGraph sub-graph for adversarial evaluator loops.

Models the planner <-> reviewer <-> judge loop as a checkpointed sub-graph
so each round gets its own checkpoint. Replaces the while-loops in
evaluator_loop.py with a 3-node StateGraph per invocation.

Nodes: adversarial_planner_node -> adversarial_reviewer_node -> judge_node
Conditional edge from judge_node: "pass"/"no_progress"/"max_rounds" -> END,
"revise" -> adversarial_planner_node (loop back).
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from pathlib import Path
from typing import Literal, Optional, TypedDict

from langgraph.graph import StateGraph, START, END
from langgraph.checkpoint.sqlite import SqliteSaver

from .evaluator_loop import (
    LoopResult,
    detect_no_progress,
    build_repair_packet,
    _make_loop_id,
    _get_stage_config_from_state,
    _category_of,
)
from .loop_events import log_loop_started, log_loop_round, log_loop_completed

log = logging.getLogger("story-lifecycle.adversarial_graph")

STORY_HOME = Path.home() / ".story-lifecycle"

# Module-level checkpoint db for adversarial sub-graphs
_adv_checkpoint_db = STORY_HOME / "adversarial_checkpoint.db"
_compiled_cache: dict[str, object] = {}
_compile_lock = threading.Lock()


class AdversarialState(TypedDict, total=False):
    """State for the adversarial sub-graph."""

    # Immutable input
    story_state: dict
    round: int
    max_rounds: int
    loop_type: str  # "plan" | "code"
    adapters: list[str]
    reviewer_model: str
    stage_output: Optional[dict]  # only for code loop

    # Mutable — filled by nodes
    plan: Optional[dict]
    review: Optional[dict]
    prev_blockers: list[dict]
    decision: Optional[str]  # pass / revise / no_progress / max_rounds / fail
    reason: str
    loop_id: str
    remaining_findings: list[str]


# ── Node: planner ──────────────────────────────────────────────


def adversarial_planner_node(state: AdversarialState) -> dict:
    """Generate or refine a plan.

    For the plan loop, calls planner.plan_stage() every round.
    For the code loop, this is a pass-through (no planning needed;
    the plan was already created in the outer graph).
    """
    from . import planner

    loop_type = state.get("loop_type", "plan")
    story_state = state.get("story_state", {})
    round_num = state.get("round", 0) + 1

    if loop_type == "code":
        # Code loop: no re-planning, just increment round counter
        return {"round": round_num}

    # Plan loop: call planner.plan_stage
    cfg = _get_stage_config_from_state(story_state)
    adapters = state.get("adapters", ["claude"])

    # Feed review context back into state for re-planning rounds
    loop_state = dict(story_state)
    review = state.get("review")
    if review and round_num > 1:
        blockers = review.get("blockers", [])
        high_blockers = [b for b in blockers if b.get("severity") == "high"]
        blocker_summary_parts = []
        for b in high_blockers:
            blocker_summary_parts.append(
                f"[{b.get('category', '')}] {b.get('description', '')}"
            )
        for s in review.get("suggestions", []):
            blocker_summary_parts.append(f"Suggestion: {s}")
        loop_state["review_summary"] = (
            f"Plan review round {round_num - 1} — revise:\n"
            + "\n".join(blocker_summary_parts)
        )

    try:
        plan = planner.plan_stage(loop_state, cfg, adapters)
    except Exception as exc:
        log.warning("plan_stage failed in adversarial round %d: %s", round_num, exc)
        return {
            "round": round_num,
            "decision": "fail",
            "reason": f"planner_error:round_{round_num}:{type(exc).__name__}",
        }

    updates: dict = {"round": round_num, "plan": plan}

    # Planner decided to skip — short-circuit
    if plan.get("skip"):
        updates["decision"] = "pass"
        updates["reason"] = "planner_skip"

    return updates


# ── Node: reviewer ─────────────────────────────────────────────


def adversarial_reviewer_node(state: AdversarialState) -> dict:
    """Review the plan (plan loop) or stage output (code loop)."""
    from . import planner

    loop_type = state.get("loop_type", "plan")
    story_state = state.get("story_state", {})
    plan = state.get("plan")
    stage_output = state.get("stage_output")
    reviewer_model = state.get("reviewer_model", "")
    cfg = _get_stage_config_from_state(story_state)

    updates: dict = {}

    if loop_type == "plan":
        if plan is None:
            # Nothing to review (e.g. planner skip already decided)
            return updates
        try:
            review = planner.review_plan(
                story_state, plan, cfg, reviewer_model=reviewer_model
            )
        except Exception as exc:
            log.warning(
                "review_plan failed in adversarial round %d, accepting plan: %s",
                state.get("round", 0),
                exc,
            )
            # Graceful degradation: accept the plan when reviewer fails
            updates["decision"] = "pass"
            updates["review"] = None
            updates["reason"] = f"reviewer_error:round_{state.get('round', 0)}:accepted"
            return updates
        updates["review"] = review

    else:
        # Code loop: review stage output
        if stage_output is None:
            return updates
        try:
            review = planner.review_stage(
                story_state, cfg, stage_output, reviewer_model=reviewer_model
            )
        except Exception as exc:
            log.warning("review_stage failed in adversarial round: %s", exc)
            updates["decision"] = "fail"
            updates["review"] = None
            updates["reason"] = f"reviewer_error:{type(exc).__name__}"
            return updates
        updates["review"] = review

        # Record findings to DB for code loop
        _record_code_findings(story_state, review)

        # Build repair packet if quality is revise
        quality = review.get("quality", "revise")
        if quality == "revise":
            _build_code_repair(state, review)

    return updates


def _record_code_findings(story_state: dict, review: dict) -> None:
    """Record issues from code review as findings in the DB."""
    from .quality import record_finding

    story_key = story_state.get("story_key", "")
    stage = story_state.get("current_stage", "")
    issues = review.get("issues", [])

    for issue in issues:
        try:
            record_finding(
                story_key,
                stage,
                {
                    "source": "code_review",
                    "severity": issue.get("severity", "medium"),
                    "category": issue.get("type", issue.get("category", "unknown")),
                    "description": issue.get("description", ""),
                    "location": issue.get("location", ""),
                    "recommendation": issue.get("recommendation", ""),
                },
            )
        except Exception:
            log.warning(
                "Failed to record finding for issue: %s",
                issue.get("description", ""),
            )


def _build_code_repair(state: AdversarialState, review: dict) -> None:
    """Build repair packet for code loop revise decisions."""
    import json as _json

    story_state = state.get("story_state", {})
    story_key = story_state.get("story_key", "")
    stage = story_state.get("current_stage", "")
    workspace = story_state.get("workspace", "")
    stage_output = state.get("stage_output", {})
    execution_count = story_state.get("execution_count", 0)

    issues = review.get("issues", [])
    try:
        repair_path = build_repair_packet(
            story_key=story_key,
            stage=stage,
            workspace=workspace,
            plan_summary=story_state.get("review_summary", ""),
            stage_output_summary=_json.dumps(stage_output, ensure_ascii=False)[:500],
            findings=[
                {
                    "severity": i.get("severity", "medium"),
                    "category": i.get("type", i.get("category", "unknown")),
                    "description": i.get("description", ""),
                    "location": i.get("location", ""),
                    "recommendation": i.get("recommendation", ""),
                }
                for i in issues
            ],
            verification={"status": "not_run", "commands": []},
            round_num=execution_count + 1,
            write_file=True,
        )
        if repair_path:
            review["repair_packet_path"] = repair_path
    except Exception as exc:
        log.warning("repair packet write failed: %s", exc)


# ── Node: judge ────────────────────────────────────────────────


def judge_node(state: AdversarialState) -> dict:
    """Evaluate the review result and decide: pass, revise, no_progress, max_rounds."""
    loop_type = state.get("loop_type", "plan")
    review = state.get("review")
    round_num = state.get("round", 0)
    max_rounds = state.get("max_rounds", 3)
    prev_blockers = state.get("prev_blockers", [])

    story_state = state.get("story_state", {})
    story_key = story_state.get("story_key", "")
    stage = story_state.get("current_stage", "")

    updates: dict = {}

    # If decision was already set (planner skip, reviewer error), pass through
    if state.get("decision") in ("pass", "fail"):
        _log_judge_round(state, state["decision"])
        return updates

    if review is None:
        # No review produced (shouldn't happen, but safety)
        updates["decision"] = "pass"
        updates["reason"] = "no_review"
        return updates

    if loop_type == "plan":
        quality = review.get("quality", "revise")
        blockers = review.get("blockers", [])
        high_blockers = [b for b in blockers if b.get("severity") == "high"]

        # No-progress detection (only after round 1)
        no_progress = False
        if round_num > 1 and high_blockers:
            no_progress = detect_no_progress(prev_blockers, high_blockers)

        if quality == "pass" or not high_blockers:
            decision = "pass"
            reason = "all_blockers_resolved"
            remaining = [
                b.get("description", "")
                for b in blockers
                if b.get("severity") != "high"
            ]
        elif no_progress:
            decision = "no_progress"
            reason = "no_progress_on_high_blockers"
            remaining = [b.get("description", "") for b in high_blockers]
        elif round_num >= max_rounds:
            decision = "max_rounds"
            reason = f"max_rounds_reached:{max_rounds}"
            remaining = [b.get("description", "") for b in high_blockers]
        else:
            decision = "revise"
            reason = ""
            remaining = []
            # Store current high blockers for next round's no-progress check
            updates["prev_blockers"] = list(high_blockers)

        _log_judge_round(state, decision, no_progress=no_progress)

        updates["decision"] = decision
        updates["reason"] = reason
        updates["remaining_findings"] = remaining

    else:
        # Code loop judge
        quality = review.get("quality", "revise")
        issues = review.get("issues", [])
        current_high = [i for i in issues if i.get("severity") == "high"]
        execution_count = story_state.get("execution_count", 0)

        # No-progress detection
        no_progress = False
        if execution_count > 0 and current_high:
            # Read previous findings from DB for no-progress detection
            from ..db import models as db

            prev_high_findings: list[dict] = []
            try:
                all_high = db.get_open_findings(story_key, min_severity="high")
                prev_high_findings = [f for f in all_high if f.get("stage") == stage]
            except Exception:
                pass
            no_progress = detect_no_progress(prev_high_findings, current_high)

            # Classify and sync resolved findings
            _sync_resolved_findings(story_key, prev_high_findings, current_high)

        if no_progress:
            decision = "wait_confirm"
            reason = "no_progress_on_high_findings"
            remaining = [i.get("description", "") for i in current_high]
        elif quality == "pass":
            decision = "pass"
            reason = "code_review_passed"
            remaining = []
        else:
            decision = quality  # "revise" or "fail"
            reason = f"code_review_{quality}"
            remaining = [i.get("description", "") for i in current_high]

        _log_judge_round(state, decision, no_progress=no_progress)

        updates["decision"] = decision
        updates["reason"] = reason
        updates["remaining_findings"] = remaining

    return updates


def _sync_resolved_findings(
    story_key: str,
    prev_high_findings: list[dict],
    current_high_issues: list[dict],
) -> None:
    """Mark previously-open findings as resolved if they weren't re-raised."""
    from .quality import update_finding_status

    prev_high_set = {
        (_category_of(f), f.get("location", "")) for f in prev_high_findings
    }
    curr_high_set = {
        (_category_of(issue), issue.get("location", ""))
        for issue in current_high_issues
    }
    resolved_keys = prev_high_set - curr_high_set
    if resolved_keys:
        for f in prev_high_findings:
            key = (_category_of(f), f.get("location", ""))
            if key in resolved_keys:
                try:
                    update_finding_status(
                        story_key,
                        f["id"],
                        "verified",
                        reason="resolved in adversarial code loop",
                    )
                except Exception:
                    log.warning(
                        "Failed to verify resolved finding %s for %s",
                        f.get("id", "?"),
                        story_key,
                    )


def _log_judge_round(
    state: AdversarialState,
    decision: str,
    *,
    no_progress: bool = False,
) -> None:
    """Log a round event for observability."""
    story_state = state.get("story_state", {})
    story_key = story_state.get("story_key", "")
    stage = story_state.get("current_stage", "")
    loop_id = state.get("loop_id", "")
    loop_type = state.get("loop_type", "plan")
    round_num = state.get("round", 0)
    review = state.get("review") or {}
    prev_blockers = state.get("prev_blockers", [])

    score = 0.0
    if review:
        if loop_type == "plan":
            blockers = review.get("blockers", [])
            score = float(
                len([b for b in blockers if b.get("severity") == "high"]) == 0
            )
        else:
            score = float(review.get("trajectory_score", 0.0))

    findings = {
        "open_before": [
            f"{b.get('category', '')}:{b.get('description', '')}" for b in prev_blockers
        ],
        "new": [],
        "resolved": [],
        "repeated": [],
    }

    log_loop_round(
        story_key=story_key,
        stage=stage,
        loop_id=loop_id,
        round_id=round_num,
        loop_type=loop_type,
        mode="sub_graph",
        decision=decision,
        score=score,
        findings=findings,
        verification={"status": "not_run", "commands": []},
        prompt_tokens={
            "total": 0,
            "context": 0,
            "feedback": 0,
            "repeated_context": 0,
            "estimated": True,
        },
        no_progress=no_progress,
    )


# ── Conditional edge ───────────────────────────────────────────


def route_judge(state: AdversarialState) -> str:
    """Route after judge: revise loops back to planner, otherwise END."""
    decision = state.get("decision", "fail")
    if decision == "revise":
        return "adversarial_planner"
    return "__end__"


# ── Graph builder ──────────────────────────────────────────────


def build_adversarial_graph(
    loop_type: Literal["plan", "code"],
) -> StateGraph:
    """Build the 3-node adversarial sub-graph.

    Nodes: adversarial_planner -> adversarial_reviewer -> judge
    Judge has a conditional edge: "revise" loops back to planner, else END.
    """
    graph = StateGraph(AdversarialState)

    graph.add_node("adversarial_planner", adversarial_planner_node)
    graph.add_node("adversarial_reviewer", adversarial_reviewer_node)
    graph.add_node("judge", judge_node)

    graph.add_edge(START, "adversarial_planner")
    graph.add_edge("adversarial_planner", "adversarial_reviewer")
    graph.add_edge("adversarial_reviewer", "judge")

    graph.add_conditional_edges(
        "judge",
        route_judge,
        {
            "adversarial_planner": "adversarial_planner",
            "__end__": END,
        },
    )

    return graph


def get_compiled_adversarial_graph(
    loop_type: Literal["plan", "code"],
    thread_id: str,
):
    """Build, compile with checkpointing, and return the adversarial sub-graph.

    Uses a separate SQLite connection + thread_id for each invocation so
    each round gets its own checkpoint.
    """
    raw_graph = build_adversarial_graph(loop_type)

    STORY_HOME.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(_adv_checkpoint_db), check_same_thread=False)
    saver = SqliteSaver(conn)
    compiled = raw_graph.compile(checkpointer=saver)
    return compiled


def run_adversarial_subgraph(
    *,
    story_state: dict,
    loop_type: Literal["plan", "code"],
    max_rounds: int,
    adapters: list[str] | None = None,
    reviewer_model: str = "",
    stage_output: dict | None = None,
) -> LoopResult:
    """Run the adversarial sub-graph and return a LoopResult.

    This is the main entry point called from graph_nodes.py.
    """
    import os

    story_key = story_state.get("story_key", "")
    stage = story_state.get("current_stage", "")
    loop_id = _make_loop_id(loop_type, stage)
    optimizer_model = os.environ.get("STORY_LLM_MODEL", "")

    log_loop_started(
        story_key=story_key,
        stage=stage,
        loop_id=loop_id,
        loop_type=loop_type,
        mode="sub_graph",
        max_rounds=max_rounds,
        optimizer_model=optimizer_model,
        reviewer_model=reviewer_model,
        attempt_id=f"{stage}:{loop_type}_loop",
    )

    # Emit activity for TUI
    if loop_type == "plan":
        from .graph import emit_plan_activity

        emit_plan_activity(story_key, "正在评估计划质量...")

    thread_id = f"{story_key}:{stage}:{loop_type}_loop:{loop_id}"

    compiled = get_compiled_adversarial_graph(loop_type, thread_id)

    initial_state: AdversarialState = {
        "story_state": story_state,
        "round": 0,
        "max_rounds": max_rounds,
        "loop_type": loop_type,
        "adapters": adapters or ["claude"],
        "reviewer_model": reviewer_model,
        "stage_output": stage_output,
        "plan": None,
        "review": None,
        "prev_blockers": [],
        "decision": None,
        "reason": "",
        "loop_id": loop_id,
        "remaining_findings": [],
    }

    config = {"configurable": {"thread_id": thread_id}}

    try:
        result_state = compiled.invoke(initial_state, config)
    except Exception as exc:
        log.warning("adversarial sub-graph failed: %s", exc)
        log_loop_completed(
            story_key=story_key,
            stage=stage,
            loop_id=loop_id,
            loop_type=loop_type,
            decision="fail",
            rounds=0,
            reason=f"subgraph_error:{type(exc).__name__}",
            remaining_findings=[],
        )
        return LoopResult(
            decision="fail",
            rounds=0,
            reason=f"subgraph_error:{type(exc).__name__}",
        )

    # Extract results from the final sub-graph state
    decision = result_state.get("decision", "fail")
    final_plan = result_state.get("plan")
    final_review = result_state.get("review")
    rounds = result_state.get("round", 0)
    reason = result_state.get("reason", "")
    remaining_findings = result_state.get("remaining_findings", [])

    log_loop_completed(
        story_key=story_key,
        stage=stage,
        loop_id=loop_id,
        loop_type=loop_type,
        decision=decision,
        rounds=rounds,
        reason=reason,
        remaining_findings=remaining_findings,
    )

    return LoopResult(
        decision=decision,
        rounds=rounds,
        final_plan=final_plan,
        final_review=final_review,
        reason=reason,
        remaining_findings=remaining_findings,
    )
