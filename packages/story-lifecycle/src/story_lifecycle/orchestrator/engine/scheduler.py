"""Scheduler Decider(层5 多 story 调度)。

按 **优先级 + 就绪态 + FIFO** 排序多个 story,替 ``graph.py`` ``max_workers=4``
的纯 FIFO 调度。纯函数(§2.2 #1),零副作用;Handler(thread pool)拿排好的序消费。

排序键 ``(ready, priority_rank, created_at)``:
1. **就绪态**:ready=True 先(blocked 的现在跑不了,排后等)。
2. **优先级**:P0 > P1 > … > P5(数字小 = 优先)。
3. **FIFO**:同优先级按 ``created_at`` 早的先。

缺字段兜底:priority 缺省 P2,ready 缺省 True,created_at 缺省 ""(排前=最老)。
"""

from __future__ import annotations

# 优先级 → 排序权重(数字小 = 优先)。未知优先级当 P2(权重 2)。
# 支持 P0-P5 **和** 真 DB 用的 high/medium/low(大小写无关)。
_PRIORITY_RANK: dict[str, int] = {
    "P0": 0,
    "P1": 1,
    "P2": 2,
    "P3": 3,
    "P4": 4,
    "P5": 5,
    "HIGH": 1,
    "MEDIUM": 2,
    "LOW": 4,
    "HIGHEST": 0,
    "LOWEST": 5,
    "NORMAL": 2,
    "CRITICAL": 0,
    "URGENT": 0,
}
_DEFAULT_RANK = 2  # P2


def decide_schedule(*, stories: list[dict]) -> list[str]:
    """Pure Decider. Order stories for execution (replaces FIFO).

    Args:
        stories: list of story dicts. Honored keys: ``story_key`` (required),
            ``priority`` (default P2), ``ready`` (default True),
            ``created_at`` (default "" = oldest, FIFO tiebreak).

    Returns:
        Ordered list of ``story_key`` (best-first).
    """

    def sort_key(s: dict):
        ready = 0 if s.get("ready", True) else 1  # ready(0) 先于 blocked(1)
        prio = (s.get("priority") or "P2").upper()
        rank = _PRIORITY_RANK.get(prio, _DEFAULT_RANK)
        created = s.get("created_at") or ""
        return (ready, rank, created)

    return [s["story_key"] for s in sorted(stories, key=sort_key)]
