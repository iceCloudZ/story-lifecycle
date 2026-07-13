"""TAPD sync service — transform SourceItems into local stories."""

from __future__ import annotations

import logging

from ...infra.db import models as db

log = logging.getLogger(__name__)


def _is_forward(current: str, target: str, story_states: dict) -> bool:
    """target 是否在 current 的 next 链上(防回退)。

    沿 story_states 的 ``next`` 字段往前遍历,与 planner.py 的状态推进方向一致。
    current/target 相等不算 forward(同级不写)。未命中链 → False(不写,防回退)。
    """
    if not story_states or current == target:
        return False
    node = story_states.get(current, {}).get("next")
    while node:
        if node == target:
            return True
        node = story_states.get(node, {}).get("next")
    return False


def _derive_tapd_type(item) -> str:
    """从 SourceItem 派生 tapd_type(story/bug/subtask)。与原新建分支逻辑一致。"""
    if item.item_type == "bug":
        return "bug"
    if item.parent_id and item.parent_id != "0":
        return "subtask"
    return "story"


def sync_tapd(
    items: list,
    workspace: str = "",
    profile: str = "minimal",
    dry_run: bool = False,
    status_only: bool = False,
    remap_lifecycle: bool = False,
) -> dict:
    """Sync TAPD SourceItems into local stories.

    Returns dict with counts: created, updated, skipped, would_create.

    remap_lifecycle: 按 profile.tapd_state_map 把 TAPD 状态映射到 lifecycle_state。
        更新分支始终启用(增量同步本就该映射);新建分支也启用(新建无防回退问题)。
        存量回填 = ``story sync --status-only``(已有 story 走更新分支,自动刷新映射)。
    """
    # 状态治理:加载 profile 的 tapd_state_map + story_states(用于 _is_forward 防回退)。
    # try/except 让无 profile 配置的环境(如测试)不崩 —— 无映射就退化为原行为。
    tapd_map: dict = {}
    story_states: dict = {}
    try:
        from ..engine.profile_loader import resolve_profile

        rp = resolve_profile(profile)
        tapd_map = rp.tapd_state_map
        story_states = rp.story_states
    except Exception:  # noqa: BLE001 — profile 加载失败不应阻断同步
        log.debug(
            "tapd_state_map unavailable for profile=%s, sync runs unmapped", profile
        )

    result = {"created": 0, "updated": 0, "skipped": 0, "would_create": 0}
    # Workspace is validated upstream (API rejects empty/relative; CLI requires
    # an explicit --workspace). We no longer fall back to the server CWD, which
    # previously stored "." as the story workspace.
    ws = workspace

    for item in items:
        existing = db.find_by_source_id(item.source, item.id)
        tapd_type = _derive_tapd_type(item)
        # TAPD → lifecycle_state 映射(tapd_type × tapd_status → lifecycle_state)。
        mapped_state = (
            tapd_map.get(tapd_type, {}).get(item.status)
            if (tapd_map and item.status)
            else None
        )

        if dry_run:
            if existing:
                result["updated"] += 1
            else:
                result["would_create"] += 1
            continue

        parent_key = ""
        if item.item_type == "bug" and item.parent_id and item.parent_id != "0":
            parent = db.find_by_source_id(item.source, item.parent_id)
            if parent:
                parent_key = parent["story_key"]

        if existing:
            updates = {}
            if item.title:
                updates["title"] = item.title
            if item.deadline:
                updates["deadline"] = item.deadline
            if item.priority:
                updates["priority"] = item.priority
            if item.owner:
                updates["owner"] = item.owner
            if item.status:
                updates["tapd_status"] = item.status
            url = item.extra.get("url", "")
            if url:
                updates["tapd_url"] = url
            if parent_key and not existing.get("parent_key"):
                updates["parent_key"] = parent_key
            # 状态治理:映射 lifecycle_state(仅前进才写,防回退)。
            if mapped_state:
                cur = existing.get("lifecycle_state") or "开发"
                if _is_forward(cur, mapped_state, story_states):
                    updates["lifecycle_state"] = mapped_state
            if updates:
                db.update_story(existing["story_key"], **updates)
            result["updated"] += 1
            log.info(f"Updated story for {item.source}:{item.id}")
        elif status_only:
            result["skipped"] += 1
        else:
            story, _ = db.upsert_story_from_source(
                source_type=item.source,
                source_id=item.id,
                title=item.title,
                workspace=ws,
                profile=profile,
                deadline=item.deadline,
                priority=item.priority,
                owner=item.owner,
                tapd_status=item.status,
                tapd_url=item.extra.get("url", ""),
                tapd_type=tapd_type,
                intake_state="candidate",
                status="idle",
                parent_key=parent_key,
            )
            # 状态治理:新建 story 按映射写初始 lifecycle_state(无防回退问题,从无到有)。
            # upsert_story_from_source 不带 lifecycle_state 参数(跟 release_train 同范式),
            # 故新建后二次 update_story。
            if mapped_state:
                db.update_story(story["story_key"], lifecycle_state=mapped_state)
            result["created"] += 1
            log.info(f"Created story {story['story_key']} for {item.source}:{item.id}")

    return result
