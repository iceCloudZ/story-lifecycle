"""成果物(deliverable)检测 + 业务状态 gate 逻辑。

成果物是驱动业务状态(待启动→开发→测试→上线→结项)推进的硬 gate。
每个成果物可:
- 自动检测是否存在(查 DB / 查 diff / 查 delivery 表)
- 人工确认(doc 类:story_doc.confirmed_by;非 doc 类:context_json._confirmed_deliverables)
- 手动跳过(存 context_json._skipped_deliverables)

这取代了旧的「stage 名成员检查」推进逻辑(planner.py 里的
``all(state.stages ⊂ _completed_stages)``),因为 profile stage 数量
(design/build/verify)和 source story_state 数量(开发/测试/上线/结项)
对不上 —— 成果物 gate 与 stage 数量无关,只看交付物是否到位。

性能注意(grok-build §2.1 contributor 边界):
- check_deliverables 只读一次 db.get_story,ctx/skipped 线程传给子函数。
- git diff 检测(include_diff_check)默认只在 /deliverables 端点(展示用)开,
  planner 的 gate_satisfied 路径关(避免每次 stage done 跑 ~2s git diff)。
"""

from __future__ import annotations

import json
import logging

from ..infra.db import models as db
from .lifecycle_state import LifecycleState

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 成果物定义(固定通用,所有 story 共用)
# ---------------------------------------------------------------------------

# 每个成果物:key(唯一标识) + label(中文) + 检测方式 + 是否需要人工确认。
DELIVERABLE_DEFS: list[dict] = [
    {
        "key": "prd",
        "label": "PRD",
        "icon": "📄",
        "doc_type": "prd",
        "needs_confirm": False,  # PRD 只需存在,不需人工确认
    },
    {
        "key": "spec",
        "label": "设计文档",
        "icon": "📝",
        "doc_type": "spec",
        "needs_confirm": True,
    },
    {
        "key": "code",
        "label": "代码变更",
        "icon": "💻",
        "diff_check": True,
        "needs_confirm": True,
    },
    {
        "key": "test_report",
        "label": "测试报告",
        "icon": "🧪",
        "doc_type": "test_report",
        "needs_confirm": True,
    },
    {
        "key": "delivery",
        "label": "上线交付",
        "icon": "🚀",
        "delivery_check": True,
        "needs_confirm": True,
    },
]

# ---------------------------------------------------------------------------
# 业务状态 gate 映射(固定写死,用 LifecycleState enum)
# ---------------------------------------------------------------------------

# (from_state, to_state) → 该转换需要满足的成果物 key 列表。
LIFECYCLE_GATES: dict[tuple[str, str], list[str]] = {
    (LifecycleState.PENDING.value, LifecycleState.DEV.value): ["prd", "spec"],
    (LifecycleState.DEV.value, LifecycleState.TEST.value): ["code"],
    (LifecycleState.TEST.value, LifecycleState.ONLINE.value): ["test_report"],
    (LifecycleState.ONLINE.value, LifecycleState.CLOSED.value): ["delivery"],
}

# 业务状态顺序(用于找下一个状态)。
LIFECYCLE_ORDER = [
    LifecycleState.PENDING.value,
    LifecycleState.DEV.value,
    LifecycleState.TEST.value,
    LifecycleState.ONLINE.value,
    LifecycleState.CLOSED.value,
]


def next_state(cur_state: str) -> str | None:
    """当前状态的下一个状态;已经是终态则 None。"""
    try:
        idx = LIFECYCLE_ORDER.index(cur_state)
    except ValueError:
        return None
    if idx + 1 < len(LIFECYCLE_ORDER):
        return LIFECYCLE_ORDER[idx + 1]
    return None


# ---------------------------------------------------------------------------
# 聚合:全部成果物状态 + gate 检查
# ---------------------------------------------------------------------------


def check_deliverables(
    story_key: str, *, include_diff_check: bool = True
) -> list[dict]:
    """返回所有成果物的状态列表(前端成果物清单用)。

    Args:
        include_diff_check: True 时 code 成果物回退到 git diff 检测(~2s)。
            False 时只用 _completed_stages 判断(快,planner gate 路径用)。
    """
    story = db.get_story(story_key)
    ctx: dict = {}
    if story:
        try:
            ctx = json.loads(story.get("context_json") or "{}")
        except (json.JSONDecodeError, TypeError):
            pass

    skipped = set(ctx.get("_skipped_deliverables", []))
    confirmed_non_doc = set(ctx.get("_confirmed_deliverables", []))
    completed_stages = ctx.get("_completed_stages", [])

    result = []
    for d in DELIVERABLE_DEFS:
        key = d["key"]
        if key in skipped:
            result.append({
                "key": key,
                "label": d["label"],
                "icon": d["icon"],
                "exists": False,
                "confirmed": False,
                "needs_confirm": d["needs_confirm"],
                "satisfied": True,  # skipped = 满足
                "skipped": True,
            })
            continue

        exists = False
        confirmed = False

        if d.get("doc_type"):
            doc = db.get_story_doc(story_key, d["doc_type"])
            exists = doc is not None
            confirmed = bool(doc and doc.get("confirmed_by"))
        elif d.get("diff_check"):
            # 优先用 _completed_stages(纯 DB,快)
            exists = any(s in completed_stages for s in ("build", "implement", "verify"))
            # 回退:git diff(贵,只在展示路径用)
            if not exists and include_diff_check:
                try:
                    from .workspace_diff import get_story_workspace_diff

                    result_diff = get_story_workspace_diff(story_key)
                    exists = not result_diff.get("is_empty", True) or len(result_diff.get("files", [])) > 0
                except Exception:
                    exists = False
            confirmed = key in confirmed_non_doc
        elif d.get("delivery_check"):
            artifacts = db.get_story_delivery_artifacts(story_key)
            exists = any(
                a.get("delivery_state") in ("merged", "abandoned") for a in artifacts
            )
            confirmed = key in confirmed_non_doc

        satisfied = exists and (not d["needs_confirm"] or confirmed)
        result.append({
            "key": key,
            "label": d["label"],
            "icon": d["icon"],
            "exists": exists,
            "confirmed": confirmed,
            "needs_confirm": d["needs_confirm"],
            "satisfied": satisfied,
            "skipped": False,
        })
    return result


def gate_satisfied(
    story_key: str, from_state: str, to_state: str
) -> tuple[bool, list[str]]:
    """检查 from→to 转换的成果物 gate 是否全部满足。

    planner gate 路径用 —— 关闭 git diff 检测(include_diff_check=False),
    避免 driver 每次 stage done 跑 ~2s git diff。code 成果物只用
    _completed_stages 判断(快);展示路径(/deliverables 端点)才开 git diff。

    Returns:
        (satisfied, missing_labels) — satisfied=True 可推进;
        missing_labels 是缺失的成果物中文名(前端显示「还差:测试报告」)。
    """
    required = LIFECYCLE_GATES.get((from_state, to_state), [])
    if not required:
        return True, []  # 没有定义 gate 的转换直接放行

    items = {d["key"]: d for d in check_deliverables(story_key, include_diff_check=False)}
    label_map = {d["key"]: d["label"] for d in DELIVERABLE_DEFS}
    missing = [
        label_map.get(k, k)
        for k in required
        if not items.get(k, {}).get("satisfied", False)
    ]
    return len(missing) == 0, missing


def gate_for_current_state(story_key: str) -> dict | None:
    """当前状态对应的 gate(前端显示「进入下一状态需要什么」)。

    展示路径 —— 开 git diff 检测(给前端完整状态)。

    Returns dict {from, to, required: [{key,label,satisfied,...}], all_satisfied}
    或 None(已终态 / 无法推进)。
    """
    story = db.get_story(story_key)
    if not story:
        return None
    cur = story.get("lifecycle_state", LifecycleState.PENDING.value)
    nxt = next_state(cur)
    if not nxt:
        return None
    required_keys = LIFECYCLE_GATES.get((cur, nxt), [])
    items = {d["key"]: d for d in check_deliverables(story_key, include_diff_check=True)}
    label_map = {d["key"]: d["label"] for d in DELIVERABLE_DEFS}
    required = [
        {
            "key": k,
            "label": label_map.get(k, k),
            "satisfied": items.get(k, {}).get("satisfied", False),
            "exists": items.get(k, {}).get("exists", False),
            "confirmed": items.get(k, {}).get("confirmed", False),
            "skipped": items.get(k, {}).get("skipped", False),
            "needs_confirm": items.get(k, {}).get("needs_confirm", False),
        }
        for k in required_keys
    ]
    return {
        "from": cur,
        "to": nxt,
        "required": required,
        "all_satisfied": all(r["satisfied"] for r in required),
    }
