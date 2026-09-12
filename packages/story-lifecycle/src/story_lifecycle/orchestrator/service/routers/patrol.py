"""routers/patrol — 生产巡检 API（docs/design-prod-patrol-integration.md Phase 2）。

五端点：items 全量替换/列表、runs 回写/历史、train 包聚合 overview。
巡检不是 lifecycle stage（上线后观察期，外部 skill/cron 触发），服务端不驱动
推进，只做登记/回写/聚合。与 Phase 1 的 gate-results 约定并存：skill 继续写
一条 prod_patrol gate 保持旧看板兼容（§4.4 推荐方案，服务端不做镜像）。
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from ....infra.db import models as db

log = logging.getLogger(__name__)

router = APIRouter(tags=["patrol"])

VALID_PATROL_RESULTS = {"PASS", "FAIL", "SKIP", "WAIVED"}


class PatrolItemIn(BaseModel):
    name: str
    # es_error_scan | es_behavior | sql_count | nacos_read | api_probe | manual
    # （自由字符串，约定集合见设计文档 §4.1，服务端不强校验——type 随 skill 演进）
    type: str = "manual"
    params: dict = Field(default_factory=dict)
    baseline: str | None = None
    pass_criteria: str = ""
    rollback_ref: str | None = None
    enabled: bool = True


class ReplacePatrolItemsRequest(BaseModel):
    items: list[PatrolItemIn] = Field(default_factory=list)


class PatrolRunItemIn(BaseModel):
    item_seq: int
    result: str
    observed: str = ""
    evidence_ref: str = ""


class CreatePatrolRunRequest(BaseModel):
    run_scope: str = ""  # 如 "train:app-1.2.32"，单 story 跑可留空
    executor: str = ""   # 如 "ai:prod-patrol" / "人工:姓名"
    summary: str = ""
    items: list[PatrolRunItemIn] = Field(default_factory=list)


def _serialize_item(it: dict) -> dict:
    return {
        "seq": it["seq"],
        "name": it["name"],
        "type": it.get("type") or "manual",
        "params": it.get("params") or {},
        "baseline": it.get("baseline"),
        "passCriteria": it.get("pass_criteria") or "",
        "rollbackRef": it.get("rollback_ref"),
        "enabled": bool(it.get("enabled")),
    }


def _serialize_run_item(ri: dict) -> dict:
    return {
        "seq": ri.get("item_seq"),
        "name": ri.get("name"),
        "result": ri.get("result"),
        "observed": ri.get("observed") or "",
        "evidenceRef": ri.get("evidence_ref") or "",
    }


def _serialize_run(run: dict) -> dict:
    return {
        "id": run["id"],
        "runScope": run.get("run_scope") or "",
        "startedAt": run.get("started_at") or "",
        "executor": run.get("executor") or "",
        "summary": run.get("summary") or "",
        "result": run.get("result"),
        "items": [_serialize_run_item(ri) for ri in run.get("items") or []],
    }


def _serialize_overview_story(entry: dict) -> dict:
    lr = entry.get("latest_run")
    return {
        "storyKey": entry["story_key"],
        "title": entry.get("title"),
        "lifecycleState": entry.get("lifecycle_state"),
        "status": entry.get("status"),
        "itemsCount": entry.get("items_count", 0),
        "latestRun": (
            {
                "id": lr["id"],
                "runScope": lr.get("run_scope") or "",
                "startedAt": lr.get("started_at") or "",
                "executor": lr.get("executor") or "",
                "summary": lr.get("summary") or "",
                "result": lr.get("result"),
                "failItems": [
                    {
                        "seq": f["item_seq"],
                        "name": f.get("name"),
                        "observed": f.get("observed") or "",
                        "evidenceRef": f.get("evidence_ref") or "",
                    }
                    for f in lr.get("fail_items") or []
                ],
            }
            if lr
            else None
        ),
    }


def _get_story_or_404(story_key: str) -> dict:
    s = db.get_story(story_key)
    if not s:
        raise HTTPException(status_code=404, detail=f"story not found: {story_key}")
    return s


@router.put("/api/story/{story_key}/patrol/items")
def put_patrol_items(story_key: str, req: ReplacePatrolItemsRequest):
    """批量替换该 story 的巡检项（全量覆盖，幂等）。seq 按传入顺序 1..N。"""
    s = _get_story_or_404(story_key)

    for i, it in enumerate(req.items, start=1):
        if not it.name.strip():
            raise HTTPException(
                status_code=400,
                detail=f"items[{i - 1}].name 不能为空",
            )

    items = db.replace_patrol_items(story_key, [it.model_dump() for it in req.items])
    db.log_event(
        story_key,
        s.get("current_stage") or "",
        "patrol_items_replaced",
        {"count": len(items)},
    )
    return {"ok": True, "storyKey": story_key, "items": [_serialize_item(it) for it in items]}


@router.get("/api/story/{story_key}/patrol/items")
def get_patrol_items(story_key: str):
    """该 story 的巡检项清单（按 seq 稳序）。"""
    _get_story_or_404(story_key)
    items = db.list_patrol_items(story_key)
    return {"storyKey": story_key, "items": [_serialize_item(it) for it in items]}


@router.post("/api/story/{story_key}/patrol/runs")
def post_patrol_run(story_key: str, req: CreatePatrolRunRequest):
    """回写一轮巡检（items 结果内联）。轮次结论 = 任一 FAIL 则 FAIL，否则 PASS。"""
    s = _get_story_or_404(story_key)

    for i, it in enumerate(req.items):
        normalized = (it.result or "").upper()
        if normalized not in VALID_PATROL_RESULTS:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"items[{i}].result 无效: {it.result}. "
                    f"Expected one of {sorted(VALID_PATROL_RESULTS)}"
                ),
            )

    run = db.create_patrol_run(
        story_key,
        run_scope=req.run_scope.strip(),
        executor=req.executor.strip(),
        summary=req.summary,
        items=[it.model_dump() for it in req.items],
    )
    db.log_event(
        story_key,
        s.get("current_stage") or "",
        "patrol_run_recorded",
        {
            "run_id": run["id"],
            "run_scope": run["run_scope"],
            "result": run["result"],
            "summary": run["summary"][:200],
        },
    )
    # WP-D:巡检 FAIL → 管家事件出口发声(interrupt 档,outbox 一行,观察)。
    # 判定键 = 服务端 rollup 结论 run["result"](任一项 FAIL 即 FAIL,与 train
    # overview / patrolSummary 徽标同源;summary 是调用方自由文本,不作信号)。
    # create_patrol_run 恒新建(请求体无 run_id,无幂等重放路径)——每次成功
    # POST 都是首次创建,天然满足「仅新建 run 告警」。emit 自吞异常,绝不影响
    # API 响应本体。
    if run.get("result") == "FAIL":
        try:
            failed_items = [
                {
                    "name": it.get("name"),
                    "result": it.get("result"),
                    "evidence_ref": it.get("evidence_ref") or "",
                }
                for it in run.get("items") or []
                if it.get("result") == "FAIL"
            ]
            scope = (run.get("run_scope") or "").strip()
            if scope.lower().startswith("train:"):
                train = scope[len("train:"):].strip()
            else:
                train = (s.get("release_train") or "")
            fail_names = (
                "、".join(str(f["name"]) for f in failed_items if f.get("name"))
                or "明细见控制台"
            )
            from ....infra.notification.emitter import emit_event

            emit_event(
                "patrol_failed",
                story_key=story_key,
                title=f"[{story_key}] 巡检 FAIL:{train or '未挂包'}",
                message=f"巡检结论 FAIL,失败 {len(failed_items)} 项:{fail_names}",
                payload={
                    "train": train,
                    "story_key": story_key,
                    "run_id": run["id"],
                    "run_scope": run.get("run_scope") or "",
                    "failed_items": failed_items,
                },
            )
        except Exception:  # noqa: BLE001 — 观察性事件,绝不影响 API 响应
            log.debug("patrol_failed emit failed (non-fatal)", exc_info=True)
    return {"ok": True, "runId": run["id"], "result": run["result"], "run": _serialize_run(run)}


@router.get("/api/story/{story_key}/patrol/runs")
def get_patrol_runs(story_key: str, limit: int = 20):
    """巡检轮次历史（新→旧），items 内联。"""
    _get_story_or_404(story_key)
    limit = max(1, min(limit, 200))
    runs = db.list_patrol_runs(story_key, limit=limit)
    return {"storyKey": story_key, "runs": [_serialize_run(r) for r in runs]}


@router.get("/api/trains/{train}/patrol/overview")
def train_patrol_overview(train: str):
    """包维度聚合：train 下全部 story 的 items 数、最新轮结论、FAIL 明细、
    从未巡检清单。巡检 skill 每轮开始 GET 一次即得本轮范围。"""
    train = (train or "").strip()
    if not train:
        raise HTTPException(status_code=400, detail="train 不能为空")
    overview = db.get_train_patrol_overview(train)
    return {
        "train": overview["train"],
        "total": overview["total"],
        "patrolled": overview["patrolled"],
        "failed": overview["failed"],
        "neverPatrolled": overview["neverPatrolled"],
        "stories": [_serialize_overview_story(e) for e in overview["stories"]],
    }
