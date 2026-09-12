"""routers/knowledge — 知识检索面(DESIGN-v1-work-agent WP-F §8.2)。

``GET /api/knowledge/search`` —— ``KnowledgeIndex.retrieve`` 的薄包装:

- 根解析走 :func:`resolve_knowledge_root`(带 story_key 时用该 story 的
  workspace 解析,读不到 story 或无 workspace 则全局根;写读同根,B4 不变量)。
- 响应条目带 ``source_refs``(知识出处,RUN 新坑指向 RUN md 绝对路径)。
- **失败降级,绝不 500**:任何异常 → 200 + ``{"results": [], "warning": "<原因>"}``
  —— 检索面是锦上添花,不能拖垮 story 主链路(设计 8.2 明文)。
"""

from __future__ import annotations

import logging

from fastapi import APIRouter

logger = logging.getLogger(__name__)

router = APIRouter(tags=["knowledge"])

_DEFAULT_TOP_K = 5
_MAX_TOP_K = 50


def _entry_to_item(entry) -> dict:
    """KnowledgeEntry → 响应条目(带 source_refs;detail/summary 按类型尽力带)。"""
    return {
        "id": entry.id,
        "type": entry.type,
        "title": entry.title,
        "category": getattr(entry, "category", ""),
        "detail": getattr(entry, "detail", ""),
        "summary": getattr(entry, "summary", ""),
        "tags": list(entry.tags or []),
        "source_refs": list(entry.source_refs or []),
        "path": entry.path or "",
    }


def _resolve_workspace_for_story(story_key: str) -> str:
    """story_key → 该 story 的 workspace(读不到返回空串 = 走全局根)。"""
    if not story_key:
        return ""
    try:
        from ....infra.db import models as db

        story = db.get_story(story_key) or {}
        return story.get("workspace") or ""
    except Exception:  # noqa: BLE001 — story 查不到不阻塞检索(降级到全局根)
        return ""


@router.get("/api/knowledge/search")
def api_knowledge_search(
    q: str = "",
    story_key: str = "",
    stage: str = "",
    top_k: int = _DEFAULT_TOP_K,
):
    """知识库检索:关键词 q + 可选 story_key/stage 上下文,默认 top 5。"""
    try:
        top_k = max(1, min(int(top_k), _MAX_TOP_K))
    except (TypeError, ValueError):
        top_k = _DEFAULT_TOP_K

    try:
        from ....knowledge.knowledge_store.paths import resolve_knowledge_root

        workspace = _resolve_workspace_for_story(story_key)
        root = resolve_knowledge_root(workspace or None)

        from knowledge import KnowledgeIndex

        idx = KnowledgeIndex(str(root))
        entries = idx.retrieve(
            query=q or "",
            story_key=story_key or "",
            stage=stage or "",
            top_k=top_k,
        )
        return {
            "query": q,
            "story_key": story_key,
            "stage": stage,
            "knowledge_root": str(root),
            "count": len(entries),
            "results": [_entry_to_item(e) for e in entries],
        }
    except Exception as exc:  # noqa: BLE001 — 降级空结果 + warning,不 500(设计 8.2)
        logger.warning("knowledge search 降级(q=%r, story_key=%r): %s", q, story_key, exc)
        return {
            "query": q,
            "story_key": story_key,
            "stage": stage,
            "count": 0,
            "results": [],
            "warning": f"知识检索不可用:{exc}",
        }
