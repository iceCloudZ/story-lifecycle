"""sourcing/aging — 超龄纯判定(DESIGN-v1-work-agent WP-C §5.2)。

Decider 纯函数:只读入参 dict 与配置,无 DB 读写、无网络、无副作用 —— 同步时
的升级事件(routers/sync.py)与日清 digest(routers/digest.py)都消费这里,
保证「挂了几天」只有一种算法。

时间字段来源:TAPD Bug API 自有字段(created/modified/resolved),经
``TapdSource._parse_bug`` 进 extra,同步时由 routers/sync.py 落 story 的
``context_json``(键 ``bug_created``/``bug_modified``/``bug_resolved``/
``bug_expected_fix_time``,update_context 约定)。

判定语义:
- ``bug_active_days``:today − effective_start。effective_start = reopened 态取
  modified(重新打开的 bug 从最近一次激活算挂龄),否则 created;created 缺失
  → None(无法判定不硬编);未来时间(时钟偏差)钳到 0。
- ``story_overdue_days``:仅真逾期才返回天数(≥1);deadline 缺失/解析失败/
  今天及以后 → None(「到期不等于逾期」,不返回 0/负数混淆调用方)。
"""

from __future__ import annotations

from datetime import date, datetime, timezone

# bug 时间字段在 story.context_json 里的键前缀(routers/sync.py 落库同源)
BUG_TIME_CONTEXT_PREFIX = "bug_"
BUG_TIME_CONTEXT_KEYS = (
    "bug_created",
    "bug_modified",
    "bug_resolved",
    "bug_expected_fix_time",
)

# config.yaml tapd.bug_aging_warn_days 缺省(挂龄 ≥ 该天数 → interrupt 提醒)
DEFAULT_BUG_AGING_WARN_DAYS = 3


def parse_day(value) -> date | None:
    """宽松日期解析:"2026-09-01[ HH:MM:SS]" / ISO 时间戳 → date;解析失败 None。

    TAPD 返回 "YYYY-MM-DD HH:MM:SS"(北京时间的字面量),取前 10 位按日期解析
    即可 —— 挂龄以天为单位,时分秒不参与。
    """
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def bug_active_days(bug: dict, *, today: date | None = None) -> int | None:
    """bug 挂龄(天):today − effective_start,≥0;created 缺失 → None。

    effective_start:status == "reopened" 且 modified 可解析 → modified
    (重新打开 = 最近一次激活),否则 created。
    """
    today = today or _today()
    created = parse_day((bug or {}).get("created"))
    if created is None:
        return None
    start = created
    if str((bug or {}).get("status") or "") == "reopened":
        modified = parse_day((bug or {}).get("modified"))
        if modified is not None:
            start = modified
    return max(0, (today - start).days)


def story_overdue_days(story: dict, *, today: date | None = None) -> int | None:
    """story 逾期天数:deadline 早于今天 → (today − deadline).days ≥ 1。

    deadline 缺失/解析失败/今天及以后 → None(未逾期)。
    """
    today = today or _today()
    deadline = parse_day((story or {}).get("deadline"))
    if deadline is None or deadline >= today:
        return None
    return (today - deadline).days


def bug_aging_warn_days(config: dict | None = None) -> int:
    """挂龄提醒阈值:config.yaml ``tapd.bug_aging_warn_days``,缺省 3。

    ``config`` 显式传入(测试/调用方已持有配置);None 读 infra.config(函数内
    import,monkeypatch 可控)。解析失败/负值 → 缺省(坏配置不炸、不关提醒)。
    """
    if config is None:
        try:
            from ..infra.config import get_config

            config = get_config()
        except Exception:  # noqa: BLE001 — 配置读取失败按无配置处理
            config = {}
    try:
        raw = ((config or {}).get("tapd") or {}).get("bug_aging_warn_days")
        value = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_BUG_AGING_WARN_DAYS
    return value if value >= 0 else DEFAULT_BUG_AGING_WARN_DAYS


def bug_time_fields(story_row: dict) -> dict:
    """从 story 行还原 bug_active_days 的入参:读 context_json 的 bug_* 键。

    同步落库(routers/sync.py)与这里共用同一组键 —— 消费方绝不另写解析。
    context_json 坏/缺键 → 空串(由 bug_active_days 判 None)。
    """
    import json

    try:
        ctx = json.loads((story_row or {}).get("context_json") or "{}")
    except (ValueError, TypeError):
        ctx = {}
    if not isinstance(ctx, dict):
        ctx = {}
    return {
        "created": ctx.get("bug_created", "") or "",
        "modified": ctx.get("bug_modified", "") or "",
        "status": (story_row or {}).get("tapd_status", "") or "",
    }


def _today() -> date:
    """判定基准"今天"。统一用 UTC 日期:outbox created_at、patrol started_at、
    daily_cmd 的简报基准全是 UTC,判定与账本同轴才不会在凌晨窗口错位一天
    (TAPD 字面量是北京时刻,白天工作时段 UTC 日期与北京一致,±0)。"""
    return datetime.now(timezone.utc).date()
