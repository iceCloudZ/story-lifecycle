"""routers/deliverables — deliverables domain API（设计15 阶段C 拆自 api.py）。"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from ....infra.db import models as db

router = APIRouter(tags=["deliverables"])


@router.get("/api/story/{story_key}/deliverables")
def api_get_deliverables(story_key: str):
    """成果物清单 + 当前 gate 状态(概览第二层进度条用)。"""
    from ....sourcing.deliverables import check_deliverables, gate_for_current_state

    return {
        "deliverables": check_deliverables(story_key),
        "gate": gate_for_current_state(story_key),
    }


@router.post("/api/story/{story_key}/deliverables/{deliv_key}/skip")
def api_skip_deliverable(story_key: str, deliv_key: str):
    """跳过某成果物(存 context_json._skipped_deliverables)。

    跳过的成果物在 gate 检查时视为 satisfied(不阻塞推进)。
    """
    import json as _json

    s = db.get_story(story_key)
    if not s:
        raise HTTPException(404, "Story not found")
    try:
        ctx = _json.loads(s.get("context_json") or "{}")
    except (ValueError, TypeError):
        ctx = {}
    skipped = set(ctx.get("_skipped_deliverables", []))
    skipped.add(deliv_key)
    ctx["_skipped_deliverables"] = sorted(skipped)
    db.update_story(story_key, context_json=_json.dumps(ctx, ensure_ascii=False))
    return {"ok": True, "skipped": sorted(skipped)}


@router.post("/api/story/{story_key}/deliverables/{deliv_key}/confirm")
def api_confirm_deliverable(story_key: str, deliv_key: str):
    """人工确认某成果物(非 doc 类,如 code/delivery)。存 context_json._confirmed_deliverables。

    doc 类(spec/test_report)走 PUT /docs/{type}/confirm(写 story_doc.confirmed_by)。
    非 doc 类(code/delivery)走本端点(写 context_json)。
    只有 user 手动调用 —— AI 不能自我确认。
    """
    import json as _json

    s = db.get_story(story_key)
    if not s:
        raise HTTPException(404, "Story not found")
    try:
        ctx = _json.loads(s.get("context_json") or "{}")
    except (ValueError, TypeError):
        ctx = {}
    confirmed = set(ctx.get("_confirmed_deliverables", []))
    confirmed.add(deliv_key)
    ctx["_confirmed_deliverables"] = sorted(confirmed)
    db.update_story(story_key, context_json=_json.dumps(ctx, ensure_ascii=False))
    return {"ok": True, "confirmed": sorted(confirmed)}
