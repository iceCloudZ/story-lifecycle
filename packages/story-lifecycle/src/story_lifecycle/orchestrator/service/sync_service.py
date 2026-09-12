"""TAPD sync service — transform SourceItems into local stories."""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from ...infra.db import models as db

log = logging.getLogger(__name__)


def _is_forward(current: str, target: str, story_states: dict) -> bool:
    """target 是否在 current 的 next 链上(防回退)。

    沿 story_states 的 ``next`` 字段往前遍历,与 planner.py 的状态推进方向一致。
    current/target 相等不算 forward(同级不写)。未命中链 → False(不写,防回退)。

    特例:「待启动」不在 story_states 拓扑里(它是规划前的前态,无 stages),但从它
    到任何已定义状态都算前进 — 新同步的 candidate 落待启动,TAPD 状态映射(如
    closed→结项、progressing→开发)应能正常写入(见 TABS-LIFECYCLE-STATE 决策)。
    """
    if not story_states or current == target:
        return False
    if current == "待启动":
        return target in story_states
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


# ---------------- WP-C: bug 时间字段落库 + 超龄升级事件 ----------------
# 这两个动作是「一次成功同步」的一部分(API 路由与 CLI `story sync` 都经
# sync_tapd,故必须住在 service 层,不能只挂在 router 上 —— 否则 CLI 主路径
# (story-loop step 1)同步完既不落时间字段也不发超龄告警)。


def persist_bug_time_fields(story_key: str, fields: dict) -> None:
    """WP-C:把 TAPD Bug 自有时间字段落 story.context_json(现有 extra 落库路径
    的核实结论:url 走 tapd_url 列,severity/related_story_id 不落库;时间字段
    按 update_context 约定进 context_json,键 bug_created/bug_modified/
    bug_resolved/bug_expected_fix_time,与 sourcing/aging.bug_time_fields 同源)。
    只写非空值(存量行不回填,下次同步自然带上)。失败吞掉不阻断同步。
    """
    from ...sourcing.aging import BUG_TIME_CONTEXT_KEYS

    pairs = (
        ("bug_created", "created"),
        ("bug_modified", "modified"),
        ("bug_resolved", "resolved"),
        ("bug_expected_fix_time", "expected_fix_time"),
    )
    try:
        for ctx_key, raw_key in pairs:
            value = str((fields or {}).get(raw_key, "") or "")
            if value and ctx_key in BUG_TIME_CONTEXT_KEYS:
                db.update_context(story_key, ctx_key, value)
    except Exception:  # noqa: BLE001 — 展示/判定辅助字段,绝不阻断同步主链
        log.debug("bug time fields persist failed for %s", story_key, exc_info=True)


def _emitted_today(event_type: str, story_key: str, today: str) -> bool:
    """当日去重查询:notification_outbox 是否已有同 event_type + story_key 且
    created_at(UTC)落在 today 的行。outbox 无内建去重,emit 前查这里。"""
    with db._db() as conn:
        row = conn.execute(
            "SELECT 1 FROM notification_outbox "
            "WHERE event_type = ? AND story_key = ? AND created_at >= ? "
            "AND created_at < date(?, '+1 day') LIMIT 1",
            (event_type, story_key, f"{today} 00:00:00", today),
        ).fetchone()
    return row is not None


def escalate_aging_events(items, tapd_config: dict) -> None:
    """WP-C:同步落库后的超龄升级事件(观察,同一天同 key 只提醒一次)。

    - bug:owner 匹配配置 owner(未配置 owner → 不过滤)且挂龄 ≥ 阈值
      (config tapd.bug_aging_warn_days,缺省 3)→ ``bug_aging``(interrupt)
    - story:deadline 已逾期 → ``story_overdue``(interrupt)

    全程 try/except 吞掉 + debug 日志 —— 升级提醒是观察性事件,绝不影响同步
    结果本体。判定只用 sourcing/aging 纯函数,不在此处复算时间差。

    ``tapd_config``:调用方已加载的 TAPD 配置段(routers/sync.py 传入);sync_tapd
    未显式传入时自取(_load_tapd_config,CLI 路径)。
    """
    try:
        from ...infra.notification.emitter import emit_event
        from ...sourcing.aging import (
            bug_active_days,
            bug_aging_warn_days,
            story_overdue_days,
        )

        # 判定基准与账本同轴:挂龄/逾期用 UTC 日期(与 sourcing.aging._today、
        # daily_cmd 简报一致);去重日必须用 UTC —— outbox.created_at 是 UTC。
        today = datetime.now(timezone.utc).date()
        today_str = today.isoformat()
        threshold = bug_aging_warn_days()
        configured_owner = str((tapd_config or {}).get("owner", "") or "").rstrip(";")

        for item in items:
            story = db.find_by_source_id(item.source, item.id)
            story_key = story["story_key"] if story else f"tapd-{item.id}"
            if item.item_type == "bug":
                age = bug_active_days(
                    {
                        "created": item.extra.get("created", ""),
                        "modified": item.extra.get("modified", ""),
                        "status": item.status,
                    },
                    today=today,
                )
                if age is None or age < threshold:
                    continue
                # owner 匹配:配置了 owner 才过滤;TAPD current_owner 可能多人
                # 分号分隔,子串匹配(与 TapdSource 的 owner 过滤同型)。
                if configured_owner and configured_owner not in (item.owner or ""):
                    continue
                if _emitted_today("bug_aging", story_key, today_str):
                    continue
                url = item.extra.get("url", "")
                emit_event(
                    "bug_aging",
                    story_key=story_key,
                    title=f"[{story_key}] bug 挂龄 {age} 天(≥{threshold}):{item.title[:40]}",
                    message=f"bug「{item.title}」已挂 {age} 天(阈值 {threshold} 天),状态 {item.status or '未知'},请跟进。",
                    payload={
                        "key": story_key,
                        "title": item.title,
                        "age_days": age,
                        "url": url,
                    },
                )
            elif item.item_type in ("requirement", "story"):
                overdue = story_overdue_days({"deadline": item.deadline}, today=today)
                if overdue is None:
                    continue
                if _emitted_today("story_overdue", story_key, today_str):
                    continue
                url = item.extra.get("url", "")
                emit_event(
                    "story_overdue",
                    story_key=story_key,
                    title=f"[{story_key}] 需求逾期 {overdue} 天:{item.title[:40]}",
                    message=f"需求「{item.title}」截止 {item.deadline} 已逾期 {overdue} 天,请跟进。",
                    payload={
                        "key": story_key,
                        "title": item.title,
                        "age_days": overdue,
                        "url": url,
                    },
                )
    except Exception:  # noqa: BLE001 — 观察性事件,绝不影响同步响应
        log.debug("aging escalation emit failed (non-fatal)", exc_info=True)


def sync_tapd(
    items: list,
    workspace: str = "",
    profile: str = "minimal",
    dry_run: bool = False,
    status_only: bool = False,
    status_names: dict[str, str] | None = None,
    tapd_config: dict | None = None,
) -> dict:
    """Sync TAPD SourceItems into local stories.

    Returns dict with counts: created, updated, skipped, would_create.

    SOURCE-DRIVEN-MODEL: 状态映射(tapd_state_map)和业务状态机(story_states)按
    source_type("tapd")从 source profile 加载,不再从 profile 读。增量同步始终启用
    映射(更新分支前进才写,新建分支从无到有)。存量回填 = ``story sync --status-only``。
    ``profile`` 参数保留仅为给新建 story 写入 profile 名(它仍是 story 的执行配置)。

    status_names: status → 中文状态名(自定义工作流的 status_N 不透明,展示需要译名)。
    调用方一次 sync 取一份传进来(routers/sync.py、cli/sync_cmd.py),best-effort。

    tapd_config: TAPD 配置段(WP-C:owner 过滤/挂龄阈值等升级事件输入)。API 路由
    传入其已加载的配置;缺省(CLI `story sync` 路径)自取 ``_load_tapd_config()``
    (story_home/config.yaml 的 tapd 段)。非 dry-run 同步结束时:
    bug 时间字段落 context_json(persist_bug_time_fields)+ 超龄升级事件
    (escalate_aging_events,当日同 key 去重)。
    """  # noqa: D301
    # 状态治理:加载 tapd source profile 的 state_map + story_states(用于 _is_forward
    # 防回退)+ pause_states(已暂缓 → 本地 pause)。try/except 让无配置的环境(如测试)
    # 不崩 —— 无映射就退化为原行为。
    tapd_map: dict = {}
    story_states: dict = {}
    pause_states: dict = {}
    try:
        from ...sourcing.source_loader import resolve_source_profile

        sp = resolve_source_profile("tapd")
        tapd_map = sp.state_map
        story_states = sp.story_states
        pause_states = sp.pause_states
    except Exception:  # noqa: BLE001 — source profile 加载失败不应阻断同步
        log.debug("tapd state_map unavailable, sync runs unmapped")

    def _pause_hit(tapd_type: str, status: str) -> bool:
        return status in set(pause_states.get(tapd_type, []) or [])

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
        status_name = (status_names or {}).get(item.status or "", "")
        is_paused_external = _pause_hit(tapd_type, item.status or "")

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
            if status_name:
                updates["tapd_status_name"] = status_name
            url = item.extra.get("url", "")
            if url:
                updates["tapd_url"] = url
            if parent_key and not existing.get("parent_key"):
                updates["parent_key"] = parent_key
            # 状态治理:映射 lifecycle_state(仅前进才写,防回退)。
            # 暂缓态不映射 lifecycle_state —— pause 语义走 story.status(下方)。
            if mapped_state and not is_paused_external:
                cur = existing.get("lifecycle_state") or "开发"
                if _is_forward(cur, mapped_state, story_states):
                    updates["lifecycle_state"] = mapped_state
            if updates:
                db.update_story(existing["story_key"], **updates)
            # WP-C:bug 时间字段落 context_json(更新分支同样刷新,幂等)
            if item.item_type == "bug":
                persist_bug_time_fields(existing["story_key"], item.extra)
            # 已暂缓旁路(SOP 全局旁路):TAPD 暂缓 → 本地 pause;离开暂缓态 →
            # 只 resume 由 TAPD 暂缓的(reason=tapd_suspended),手动 pause 不动。
            if is_paused_external and existing.get("status") == "active":
                from ...sourcing.state_machine import pause as sm_pause

                sm_pause(existing["story_key"], reason="tapd_suspended")
                log.info(f"Paused story {existing['story_key']} (TAPD 已暂缓)")
            elif (
                not is_paused_external
                and existing.get("status") == "paused"
                and _ctx_pause_reason(existing) == "tapd_suspended"
            ):
                from ...sourcing.state_machine import activate as sm_activate

                sm_activate(existing["story_key"], clear_pause_reason=True)
                log.info(f"Resumed story {existing['story_key']} (TAPD 离开暂缓态)")
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
                tapd_status_name=status_name,
                tapd_url=item.extra.get("url", ""),
                tapd_type=tapd_type,
                intake_state="candidate",
                status="active",
                parent_key=parent_key,
            )
            # WP-C:新建 bug 时间字段随建随落 context_json
            if item.item_type == "bug":
                persist_bug_time_fields(story["story_key"], item.extra)
            # 状态治理:新建 story 按映射写初始 lifecycle_state(无防回退问题,从无到有)。
            # upsert_story_from_source 不带 lifecycle_state 参数(跟 release_train 同范式),
            # 故新建后二次 update_story。
            if mapped_state and not is_paused_external:
                db.update_story(story["story_key"], lifecycle_state=mapped_state)
            # 新同步进来就已是暂缓态 → 直接 pause(编排线程 no-op,等人重启)。
            if is_paused_external:
                from ...sourcing.state_machine import pause as sm_pause

                sm_pause(story["story_key"], reason="tapd_suspended")
            # sourced 创建统一打 task_type(飞轮注入门槛);批量 sync 用关键词档
            from .story_service import ensure_task_type

            ensure_task_type(
                story["story_key"],
                title=item.title,
                description=getattr(item, "description", "") or "",
                use_llm=False,
            )
            result["created"] += 1
            log.info(f"Created story {story['story_key']} for {item.source}:{item.id}")

    # WP-C:同步落库完成 → 超龄升级事件(bug_aging / story_overdue,interrupt 档,
    # 当日同 key 去重)。观察性,吞异常,绝不影响同步结果本体。dry_run 不发
    # (没落库,无 story_key 可归属,与「同步成功」前提一致)。
    if not dry_run:
        if tapd_config is None:
            from ._shared import _load_tapd_config

            tapd_config = _load_tapd_config()
        escalate_aging_events(items, tapd_config)

    return result


def _ctx_pause_reason(story: dict) -> str:
    """读 story.context_json._pause_reason(坏 JSON/无值 → "")。"""
    import json

    try:
        ctx = json.loads(story.get("context_json") or "{}")
    except (ValueError, TypeError):
        return ""
    return ctx.get("_pause_reason", "") if isinstance(ctx, dict) else ""
