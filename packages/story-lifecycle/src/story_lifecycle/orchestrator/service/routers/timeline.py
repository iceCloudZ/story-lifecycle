"""routers/timeline — timeline domain API（设计15 阶段C 拆自 api.py）。"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from ....infra.db import models as db
from pydantic import BaseModel, Field

router = APIRouter(tags=["timeline"])


class CreateGateResultRequest(BaseModel):
    stage: str
    gate_name: str
    result: str
    summary: str = ""
    evidence_ref: str = ""
    evidence: dict = Field(default_factory=dict)


@router.get("/api/story/{story_key}/timeline")
def get_story_timeline(story_key: str):
    """Return the complete stage timeline for a story.

    Aggregates from stage_log + event_log to produce per-stage
    status, duration, plan/review summaries, gate decisions,
    loop rounds, and trajectory score.
    """
    s = db.get_story(story_key)
    if not s:
        raise HTTPException(404, "Story not found")

    stage_logs = db.get_stage_logs(story_key, limit=200)
    events = db.get_story_events(story_key)

    # Group events by stage
    events_by_stage: dict[str, list[dict]] = {}
    for ev in events:
        stage = ev.get("stage", "")
        events_by_stage.setdefault(stage, []).append(ev)

    # Build per-stage timeline entries
    stages_seen: dict[str, dict] = {}
    for sl in stage_logs:
        stage = sl.get("stage", "")
        if stage not in stages_seen:
            stages_seen[stage] = {
                "stage": stage,
                "status": "",
                "started_at": None,
                "completed_at": None,
                "duration_ms": None,
                "plan_summary": "",
                "review_summary": "",
                "gate_decisions": [],
                "loop_rounds": 0,
                "trajectory_score": None,
                "events": [],
            }
        entry = stages_seen[stage]
        action = sl.get("action", "")
        if action == "complete":
            entry["status"] = "completed"
            entry["completed_at"] = sl.get("created_at")
        elif action == "retry":
            entry["status"] = "retrying"
        elif action == "skip":
            entry["status"] = "skipped"
            entry["completed_at"] = sl.get("created_at")
        elif action == "fail":
            entry["status"] = "failed"
            entry["completed_at"] = sl.get("created_at")
        elif action == "pause":
            entry["status"] = "paused"
        if not entry["started_at"]:
            entry["started_at"] = sl.get("created_at")

    # Fill from events
    for stage, stage_events in events_by_stage.items():
        if stage not in stages_seen:
            stages_seen[stage] = {
                "stage": stage,
                "status": "active",
                "started_at": None,
                "completed_at": None,
                "duration_ms": None,
                "plan_summary": "",
                "review_summary": "",
                "gate_decisions": [],
                "loop_rounds": 0,
                "trajectory_score": None,
                "events": [],
            }
        entry = stages_seen[stage]
        for ev in stage_events:
            ev_type = ev.get("event_type", "")
            payload = db.parse_event_payload(ev)

            if ev_type == "plan":
                if payload.get("summary"):
                    entry["plan_summary"] = payload["summary"]
                if payload.get("trajectory_score") is not None:
                    entry["trajectory_score"] = payload["trajectory_score"]
                if payload.get("loop_rounds"):
                    entry["loop_rounds"] = max(
                        entry["loop_rounds"], payload["loop_rounds"]
                    )
            elif ev_type == "review":
                if payload.get("summary"):
                    entry["review_summary"] = payload["summary"]
            elif ev_type == "gate_decision":
                entry["gate_decisions"].append(payload)

            # Key events summary
            if ev_type in (
                "plan",
                "review",
                "gate_decision",
                "route_decision",
                "node_error",
                "validation_failure",
            ):
                entry["events"].append(
                    {
                        "event_type": ev_type,
                        "created_at": ev.get("created_at"),
                        "summary": payload.get("summary", payload.get("reason", ""))[
                            :100
                        ],
                    }
                )

    # Compute duration for completed stages
    for entry in stages_seen.values():
        if entry["started_at"] and entry["completed_at"]:
            try:
                from datetime import datetime

                start = datetime.fromisoformat(entry["started_at"])
                end = datetime.fromisoformat(entry["completed_at"])
                entry["duration_ms"] = int((end - start).total_seconds() * 1000)
            except Exception:
                pass

    # Order stages by their first appearance in stage_logs
    stage_order = []
    for sl in reversed(stage_logs):
        stage = sl.get("stage", "")
        if stage and stage not in stage_order:
            stage_order.append(stage)
    stage_order.reverse()

    # Add any stages only in events
    for stage in stages_seen:
        if stage and stage not in stage_order:
            stage_order.append(stage)

    result_stages = [stages_seen[s] for s in stage_order if s in stages_seen]

    # Mark current stage
    for entry in result_stages:
        if entry["stage"] == s["current_stage"] and not entry["status"]:
            entry["status"] = s["status"]

    return {"story_key": story_key, "stages": result_stages}


@router.get("/api/story/{story_key}/gate-history")
def get_gate_history(story_key: str):
    """Return the gate decision chain for a story."""
    s = db.get_story(story_key)
    if not s:
        raise HTTPException(404, "Story not found")

    events = db.get_story_events(story_key)
    decisions = []
    for ev in events:
        if ev.get("event_type") != "gate_decision":
            continue
        payload = db.parse_event_payload(ev)
        # 迭代 2（P2-UI）：透传 verdict/findings/repair_action/fallback（_log_gate_event
        # 已补全 payload；旧数据缺字段时 get 兜底）。
        decisions.append(
            {
                "decision_id": payload.get("decision_id", ""),
                "stage": ev.get("stage", ""),
                "decision": payload.get("decision", ""),
                "verdict": payload.get("verdict", ""),
                "reason_code": payload.get("reason_code", ""),
                "human_message": payload.get("reason", payload.get("human_message", "")),
                "findings": payload.get("findings", []),
                "repair_action": payload.get("repair_action", None),
                "fallback": bool(payload.get("fallback")),
                "evidence": payload.get("evidence", {}),
                "allowed_actions": payload.get("allowed_actions", []),
                "created_at": ev.get("created_at", ""),
            }
        )

    # 迭代 2（P2-UI）：stage_completion 的编排决策（approve/reject/escalate，
    # 含 [FALLBACK]/[PATH-MISS] 标记）并入时间线——设计阶段的质量判定此前
    # 只落 orchestrator_decision 表、UI 无展示（round 3 Bug #2）。
    try:
        decisions_d = db.get_decisions(story_key, limit=100)
        for d in decisions_d:
            decisions.append(
                {
                    "decision_id": d.get("id", ""),
                    "stage": d.get("stage", ""),
                    "decision": d.get("decision", ""),
                    "verdict": d.get("decision", ""),
                    "reason_code": d.get("trigger", ""),
                    "human_message": d.get("reason", ""),
                    "findings": [],
                    "repair_action": None,
                    "fallback": "fallback" in (d.get("action_taken") or "") or str(d.get("reason", "")).startswith("[FALLBACK]"),
                    "evidence": {"action_taken": d.get("action_taken", ""), "llm_model": d.get("llm_model", "")},
                    "allowed_actions": [],
                    "created_at": d.get("decided_at", ""),
                }
            )
    except Exception:  # noqa: BLE001 — 编排决策合并 best-effort
        pass

    # Also include gate_result table entries
    gate_results = db.get_gate_results(story_key, limit=100)
    for gr in gate_results:
        detail = gr.get("detail", "")
        import json as _json2

        try:
            detail_data = _json2.loads(detail) if detail else {}
        except Exception:
            detail_data = {}
        evidence = detail_data.get("evidence", {}) or {}
        if detail_data.get("evidence_ref"):
            evidence = {**evidence, "evidence_ref": detail_data["evidence_ref"]}
        decisions.append(
            {
                "decision_id": detail_data.get("decision_id", ""),
                "stage": gr.get("stage", ""),
                "decision": gr.get("result", ""),
                "verdict": detail_data.get("verdict", gr.get("result", "")),
                "reason_code": detail_data.get("reason_code", gr.get("gate_name", "")),
                "human_message": detail_data.get("summary", detail_data.get("reason", "")),
                "findings": detail_data.get("findings", []),
                "repair_action": detail_data.get("repair_action", None),
                "fallback": bool(detail_data.get("fallback")) or str(detail_data.get("reason", "")).startswith("[FALLBACK]"),
                "evidence": evidence,
                "allowed_actions": [],
                "created_at": gr.get("created_at", ""),
            }
        )

    decisions.sort(key=lambda d: d.get("created_at", ""))
    return {"decisions": decisions}


@router.post("/api/story/{story_key}/gate-results")
def api_create_gate_result(story_key: str, req: CreateGateResultRequest):
    """Record a manual gate result with evidence for story-led delivery."""
    if not db.get_story(story_key):
        raise HTTPException(status_code=404, detail=f"story not found: {story_key}")

    normalized_result = req.result.upper()
    valid_results = {"PASS", "FAIL", "BLOCKED", "PARTIAL", "WAIVED"}
    if normalized_result not in valid_results:
        raise HTTPException(
            status_code=400,
            detail=f"invalid result: {req.result}. Expected one of {sorted(valid_results)}",
        )

    import json as _json

    detail = {
        "reason_code": req.gate_name,
        "summary": req.summary,
        "evidence_ref": req.evidence_ref,
        "evidence": req.evidence or {},
    }
    db.record_gate_result(
        story_key=story_key,
        stage=req.stage,
        gate_name=req.gate_name,
        result=normalized_result,
        detail=_json.dumps(detail, ensure_ascii=False),
    )
    db.log_event(
        story_key,
        stage=req.stage,
        event_type="gate_result_recorded",
        payload={
            "gate_name": req.gate_name,
            "result": normalized_result,
            "summary": req.summary,
            "evidence_ref": req.evidence_ref,
        },
    )
    return {"ok": True, "result": normalized_result}


@router.get("/api/story/{story_key}/dependency-graph")
def get_dependency_graph(story_key: str):
    """Return sub-story DAG for a parent story."""
    s = db.get_story(story_key)
    if not s:
        raise HTTPException(404, "Story not found")

    nodes = []
    edges = []

    # Add parent node
    nodes.append(
        {
            "key": story_key,
            "status": s["status"],
            "stage": s["current_stage"],
            "title": s.get("title", ""),
        }
    )

    # Add sub-story nodes
    subs = db.get_sub_stories(story_key) or []
    for sub in subs:
        sub_key = sub["story_key"]
        nodes.append(
            {
                "key": sub_key,
                "status": sub["status"],
                "stage": sub["current_stage"],
                "title": sub.get("title", ""),
                "sub_type": sub.get("sub_type", ""),
            }
        )
        # Edge from parent to sub
        edges.append({"from": story_key, "to": sub_key})

    # Check for deeper sub-stories (2 levels)
    for sub in subs:
        sub_key = sub["story_key"]
        deeper_subs = db.get_sub_stories(sub_key) or []
        for ds in deeper_subs:
            ds_key = ds["story_key"]
            # Avoid duplicate nodes
            if not any(n["key"] == ds_key for n in nodes):
                nodes.append(
                    {
                        "key": ds_key,
                        "status": ds["status"],
                        "stage": ds["current_stage"],
                        "title": ds.get("title", ""),
                        "sub_type": ds.get("sub_type", ""),
                    }
                )
            edges.append({"from": sub_key, "to": ds_key})

    return {"nodes": nodes, "edges": edges}
