"""成果物(deliverable)检测 + 业务状态 gate 逻辑。

成果物是驱动业务状态(待启动→开发→测试→上线→结项)推进的硬 gate。
每个成果物可:
- 自动检测是否存在(查 DB / 查 diff / 查 delivery 表)
- 人工确认(doc 类:story_doc.confirmed_by)
- 手动跳过(存 context_json._skipped_deliverables)

这取代了旧的「stage 名成员检查」推进逻辑(planner.py 里的
``all(state.stages ⊂ _completed_stages)``),因为 profile stage 数量
(design/build/verify)和 source story_state 数量(开发/测试/上线/结项)
对不上 —— 成果物 gate 与 stage 数量无关,只看交付物是否到位。
"""

from __future__ import annotations

import json
import logging

from ..infra.db import models as db

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
# 业务状态 gate 映射(固定写死)
# ---------------------------------------------------------------------------

# (from_state, to_state) → 该转换需要满足的成果物 key 列表。
# 推进前检查这些 key 是否全部 satisfied(exists + confirmed 或 skipped)。
LIFECYCLE_GATES: dict[tuple[str, str], list[str]] = {
    ("待启动", "开发"): ["prd", "spec"],
    ("开发", "测试"): ["code"],
    ("测试", "上线"): ["test_report"],
    ("上线", "结项"): ["delivery"],
}

# 业务状态顺序(用于找下一个状态)。
LIFECYCLE_ORDER = ["待启动", "开发", "测试", "上线", "结项"]


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
# 单个成果物检测
# ---------------------------------------------------------------------------


def _check_doc_deliverable(story_key: str, d: dict) -> dict:
    """doc 类成果物:查 story_doc 是否有行 + confirmed_by 是否非空。"""
    doc = db.get_story_doc(story_key, d["doc_type"])
    exists = doc is not None
    confirmed = bool(doc and doc.get("confirmed_by"))
    return {
        "key": d["key"],
        "label": d["label"],
        "icon": d["icon"],
        "exists": exists,
        "confirmed": confirmed,
        "needs_confirm": d["needs_confirm"],
        "satisfied": exists and (not d["needs_confirm"] or confirmed),
    }


def _check_diff_deliverable(story_key: str, d: dict) -> dict:
    """代码变更成果物:检查 _completed_stages 含 build/implement,或 diff files>0。

    优先用 _completed_stages(纯 DB,快);如果 build 没在 completed 里但代码
    确实改了(半自动 / 手动提交),回退到 workspace_diff 检测。
    """
    story = db.get_story(story_key)
    ctx = {}
    if story:
        try:
            ctx = json.loads(story.get("context_json") or "{}")
        except (json.JSONDecodeError, TypeError):
            pass

    completed = ctx.get("_completed_stages", [])
    # build / implement / verify 阶段完成 → 代码大概率改了
    exists = any(s in completed for s in ("build", "implement", "verify"))

    # 回退:如果 stage 没标记完成,试 workspace_diff(git 比较,较慢)
    if not exists:
        try:
            from .workspace_diff import get_story_workspace_diff

            result = get_story_workspace_diff(story_key)
            exists = not result.get("is_empty", True) or len(result.get("files", [])) > 0
        except Exception:
            # workspace 不存在 / git 错误 → 当作没代码
            exists = False

    # code 的"确认"存在 context_json 里(没有 story_doc 行)
    skipped = _get_skipped(story_key)
    confirmed = d["key"] in ctx.get("_confirmed_deliverables", [])
    return {
        "key": d["key"],
        "label": d["label"],
        "icon": d["icon"],
        "exists": exists,
        "confirmed": confirmed,
        "needs_confirm": d["needs_confirm"],
        "satisfied": exists and (not d["needs_confirm"] or confirmed) or d["key"] in skipped,
    }


def _check_delivery_deliverable(story_key: str, d: dict) -> dict:
    """上线交付成果物:查 story_delivery_artifact 有 merged 记录。"""
    artifacts = db.get_story_delivery_artifacts(story_key)
    exists = any(
        a.get("delivery_state") in ("merged", "abandoned") for a in artifacts
    )
    ctx = {}
    story = db.get_story(story_key)
    if story:
        try:
            ctx = json.loads(story.get("context_json") or "{}")
        except (json.JSONDecodeError, TypeError):
            pass
    confirmed = d["key"] in ctx.get("_confirmed_deliverables", [])
    skipped = _get_skipped(story_key)
    return {
        "key": d["key"],
        "label": d["label"],
        "icon": d["icon"],
        "exists": exists,
        "confirmed": confirmed,
        "needs_confirm": d["needs_confirm"],
        "satisfied": exists and (not d["needs_confirm"] or confirmed) or d["key"] in skipped,
    }


def _get_skipped(story_key: str) -> set[str]:
    """读 context_json._skipped_deliverables(手动跳过的成果物 key 集合)。"""
    story = db.get_story(story_key)
    if not story:
        return set()
    try:
        ctx = json.loads(story.get("context_json") or "{}")
    except (json.JSONDecodeError, TypeError):
        return set()
    return set(ctx.get("_skipped_deliverables", []))


# ---------------------------------------------------------------------------
# 聚合:全部成果物状态 + gate 检查
# ---------------------------------------------------------------------------


def check_deliverables(story_key: str) -> list[dict]:
    """返回所有成果物的状态列表(前端成果物清单用)。"""
    skipped = _get_skipped(story_key)
    result = []
    for d in DELIVERABLE_DEFS:
        if d["key"] in skipped:
            result.append({
                "key": d["key"],
                "label": d["label"],
                "icon": d["icon"],
                "exists": False,
                "confirmed": False,
                "needs_confirm": d["needs_confirm"],
                "satisfied": True,  # skipped = 满足
                "skipped": True,
            })
            continue
        if d.get("doc_type"):
            item = _check_doc_deliverable(story_key, d)
        elif d.get("diff_check"):
            item = _check_diff_deliverable(story_key, d)
        elif d.get("delivery_check"):
            item = _check_delivery_deliverable(story_key, d)
        else:
            continue
        item["skipped"] = False
        result.append(item)
    return result


def gate_satisfied(
    story_key: str, from_state: str, to_state: str
) -> tuple[bool, list[str]]:
    """检查 from→to 转换的成果物 gate 是否全部满足。

    Returns:
        (satisfied, missing_labels) — satisfied=True 可推进;
        missing_labels 是缺失的成果物中文名(前端显示「还差:测试报告」)。
    """
    required = LIFECYCLE_GATES.get((from_state, to_state), [])
    if not required:
        return True, []  # 没有定义 gate 的转换直接放行

    items = {d["key"]: d for d in check_deliverables(story_key)}
    label_map = {d["key"]: d["label"] for d in DELIVERABLE_DEFS}
    missing = [
        label_map.get(k, k)
        for k in required
        if not items.get(k, {}).get("satisfied", False)
    ]
    return len(missing) == 0, missing


def gate_for_current_state(story_key: str) -> dict | None:
    """当前状态对应的 gate(前端显示「进入下一状态需要什么」)。

    Returns dict {from, to, required: [{key,label,satisfied}], all_satisfied}
    或 None(已终态 / 无法推进)。
    """
    story = db.get_story(story_key)
    if not story:
        return None
    cur = story.get("lifecycle_state", "待启动")
    nxt = next_state(cur)
    if not nxt:
        return None
    required_keys = LIFECYCLE_GATES.get((cur, nxt), [])
    items = {d["key"]: d for d in check_deliverables(story_key)}
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
