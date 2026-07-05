"""Tests for scheduler Decider(层5 多 story 调度)。

``decide_schedule`` 按 **优先级 + 就绪态 + FIFO** 排序多个 story,替 ``graph.py``
``max_workers=4`` 的纯 FIFO。纯函数,零副作用。

排序键:(就绪, 优先级, 创建时间)—— 就绪的先跑(blocked 的现在跑不了),
同就绪按优先级(P0 最高),同优先级 FIFO(created_at 早的先)。
"""

from story_lifecycle.orchestrator.engine.scheduler import decide_schedule


def st(key, priority="P2", ready=True, created_at="2026-01-01 00:00:00"):
    return {"story_key": key, "priority": priority, "ready": ready, "created_at": created_at}


class TestDecideSchedule:
    def test_empty_returns_empty(self):
        assert decide_schedule(stories=[]) == []

    def test_higher_priority_first(self):
        """P0 排在 P2 前(都就绪)。"""
        order = decide_schedule(stories=[st("A", "P2"), st("B", "P0"), st("C", "P1")])
        assert order[0] == "B"  # P0
        assert order[1] == "C"  # P1
        assert order[2] == "A"  # P2

    def test_ready_before_blocked_even_if_lower_priority(self):
        """就绪的 P2 跑在 blocked 的 P0 前(blocked 现在跑不了)。"""
        order = decide_schedule(
            stories=[st("BLOCKED", "P0", ready=False), st("READY", "P2", ready=True)]
        )
        assert order[0] == "READY"

    def test_equal_priority_is_fifo_by_created_at(self):
        """同优先级 → created_at 早的先(FIFO)。"""
        order = decide_schedule(
            stories=[
                st("LATE", "P2", created_at="2026-03-01 00:00:00"),
                st("EARLY", "P2", created_at="2026-01-01 00:00:00"),
                st("MID", "P2", created_at="2026-02-01 00:00:00"),
            ]
        )
        assert order == ["EARLY", "MID", "LATE"]

    def test_missing_priority_defaults_to_p2(self):
        """缺 priority → 当 P2(不崩,不抢占 P0/P1)。"""
        order = decide_schedule(
            stories=[
                {"story_key": "X", "ready": True, "created_at": "t1"},  # 无 priority
                st("P0STORY", "P0", created_at="t2"),
            ]
        )
        assert order[0] == "P0STORY"
        assert order[1] == "X"

    def test_high_medium_low_priority_words_ranked(self):
        """真 DB 用 high/medium/low(非 P0-P5)→ 也要能排序(否则全当 P2 退化 FIFO)。"""
        order = decide_schedule(stories=[
            st("LO", "low", created_at="t1"),
            st("HI", "high", created_at="t2"),
            st("MD", "medium", created_at="t3"),
        ])
        assert order[0] == "HI"
        assert order[1] == "MD"
        assert order[2] == "LO"

    def test_returns_only_story_keys(self):
        order = decide_schedule(stories=[st("A", "P1"), st("B", "P3")])
        assert all(isinstance(k, str) for k in order)
        assert set(order) == {"A", "B"}


from story_lifecycle.infra.db import models as db
from story_lifecycle.orchestrator.engine import graph


def test_order_ready_stories_by_priority():
    """graph.order_ready_stories 按 decide_schedule 优先级排(替 FIFO)。"""
    for k, p in (("LO", "P2"), ("HI", "P0"), ("MID", "P1")):
        db.create_story(k, k, "")
        db.update_story(k, priority=p)
    ordered = graph.order_ready_stories(["LO", "HI", "MID"])
    assert ordered[0] == "HI"  # P0 最高
    assert ordered[1] == "MID"  # P1
    assert ordered[2] == "LO"  # P2


def test_order_ready_stories_empty():
    assert graph.order_ready_stories([]) == []


def test_order_ready_stories_missing_story_row_kept():
    """某个 key 在 db 里查不到(已删)→ 不崩,其余照排。"""
    db.create_story("KEEP", "keep", "")
    db.update_story("KEEP", priority="P0")
    ordered = graph.order_ready_stories(["GONE", "KEEP"])
    assert ordered == ["KEEP"]  # GONE 被丢,KEEP 保留

