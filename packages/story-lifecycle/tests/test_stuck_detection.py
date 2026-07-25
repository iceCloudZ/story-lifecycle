"""1.7c — 规则卡住检测 + escalate_human 测试(纯确定性,零 LLM)。

设计依据:DESIGN-artifact-driven-stage-completion §4.3 / STEP 1 红线。
detect_stuck 是纯规则 Resolver(零 LLM);escalate_stuck 是 Handler(落 awaiting_confirm +
桌面通知)。三条规则:超时无新输出 / 启动卡死(归调用方)/ 反复报错。
"""

from __future__ import annotations

from story_lifecycle.orchestrator.engine.supervisor import (
    STUCK_REPEATED_ERRORS,
    STUCK_TIMEOUT_SECONDS,
    detect_stuck,
    escalate_stuck,
)


# ---- detect_stuck:纯规则 ----


def test_no_stuck_when_recent_output():
    """最近有输出 → 不卡。"""
    result = detect_stuck(
        last_output_ts=1000.0,
        now_ts=1100.0,
        process_alive=True,
    )
    assert result is None


def test_stuck_on_no_output_timeout():
    """超时无新输出(idle > timeout)→ 卡住。"""
    result = detect_stuck(
        last_output_ts=1000.0,
        now_ts=1000.0 + STUCK_TIMEOUT_SECONDS + 1,
        process_alive=True,
    )
    assert result is not None
    assert result["rule"] == "no_output_timeout"
    assert result["duration"] > STUCK_TIMEOUT_SECONDS


def test_not_stuck_when_process_dead():
    """进程死了 → 不算"卡住"(那是死了,归别处)。”
    即使 idle 超 timeout,process_alive=False → 返回 None。
    """
    result = detect_stuck(
        last_output_ts=1000.0,
        now_ts=1000.0 + STUCK_TIMEOUT_SECONDS + 100,
        process_alive=False,
    )
    assert result is None


def test_stuck_custom_timeout():
    """自定义 timeout 阈值生效。"""
    result = detect_stuck(
        last_output_ts=1000.0,
        now_ts=1010.0,  # idle 10s
        process_alive=True,
        timeout_seconds=5,  # 阈值 5s → idle 10s 卡住
    )
    assert result is not None
    assert result["rule"] == "no_output_timeout"


def test_stuck_never_output_not_flagged_here():
    """从未输出(last_output_ts=None)+ 活着 → 不在此判(启动宽限归调用方)。"""
    result = detect_stuck(
        last_output_ts=None,
        now_ts=10000.0,
        process_alive=True,
    )
    assert result is None


# ---- detect_stuck:反复报错规则 ----


def test_stuck_on_repeated_errors():
    """events.jsonl 末尾连续 >= STUCK_REPEATED_ERRORS 条 error → 反复报错卡住。"""
    events = [
        {"type": "text", "text": "正常输出"},
        {"type": "text", "text": "error: something failed"},
        {"type": "text", "text": "error: again"},
        {"type": "text", "text": "error: third"},
        {"type": "text", "text": "error: fourth"},
        {"type": "text", "text": "error: fifth"},
    ]
    result = detect_stuck(
        last_output_ts=1000.0,
        now_ts=1001.0,  # idle 很短,但反复报错命中
        process_alive=True,
        events=events,
    )
    assert result is not None
    assert result["rule"] == "repeated_errors"
    assert result["consecutive_errors"] >= STUCK_REPEATED_ERRORS


def test_not_stuck_when_errors_below_threshold():
    """连续 error < 阈值 → 不卡。"""
    events = [
        {"type": "text", "text": "error: one"},
        {"type": "text", "text": "error: two"},
    ]
    result = detect_stuck(
        last_output_ts=1000.0,
        now_ts=1001.0,
        process_alive=True,
        events=events,
    )
    assert result is None


def test_repeated_errors_broken_by_normal_output():
    """连续 error 被正常输出打断 → 重新数,不卡。"""
    events = [
        {"type": "text", "text": "error: 1"},
        {"type": "text", "text": "error: 2"},
        {"type": "text", "text": "正常进度"},  # 打断
        {"type": "text", "text": "error: 3"},
        {"type": "text", "text": "error: 4"},
    ]
    result = detect_stuck(
        last_output_ts=1000.0,
        now_ts=1001.0,
        process_alive=True,
        events=events,
    )
    assert result is None


def test_repeated_errors_detects_traceback():
    """text 含 traceback 也算 error 行(常见 Python 错误)。"""
    events = [
        {"type": "text", "text": "Traceback (most recent call last)"},
        {"type": "text", "text": "Traceback (most recent call last)"},
        {"type": "text", "text": "Traceback (most recent call last)"},
        {"type": "text", "text": "Traceback (most recent call last)"},
        {"type": "text", "text": "Traceback (most recent call last)"},
    ]
    result = detect_stuck(
        last_output_ts=1000.0,
        now_ts=1001.0,
        process_alive=True,
        events=events,
    )
    assert result is not None
    assert result["rule"] == "repeated_errors"


# ---- escalate_stuck:Handler(零 LLM,落 awaiting_confirm + 通知) ----


def test_escalate_stuck_logs_awaiting_confirm_event():
    """规则检测到卡住 → 落 awaiting_confirm 事件(payload 含 stuck_reason)。"""
    logged = []

    def fake_log_event(story_key, stage, event_type, payload):
        logged.append((story_key, stage, event_type, payload))

    escalate_stuck(
        story_key="STUCK-1",
        stage="design",
        adapter="claude",
        detection={"rule": "no_output_timeout", "reason": "idle 600s", "duration": 600},
        log_event_fn=fake_log_event,
        notify_fn=lambda t, m: None,  # 注入 no-op 通知,不调真 plyer
    )
    assert len(logged) == 1
    sk, st, et, payload = logged[0]
    assert sk == "STUCK-1"
    assert st == "design"
    assert et == "awaiting_confirm"
    assert payload["stuck"] is True
    assert payload["rule"] == "no_output_timeout"
    assert payload["stuck_reason"] == "idle 600s"


def test_escalate_stuck_sends_notification():
    """规则检测到卡住 → 发桌面通知(注入 notify_fn 断言被调)。"""
    notified = []
    escalate_stuck(
        story_key="STUCK-2",
        stage="build",
        adapter="kimi",
        detection={"rule": "repeated_errors", "reason": "反复报错 5 次"},
        log_event_fn=lambda *a, **k: None,
        notify_fn=lambda title, msg: notified.append((title, msg)),
    )
    assert len(notified) == 1
    title, msg = notified[0]
    assert "STUCK-2" in title
    assert "反复报错" in msg


def test_escalate_stuck_notify_failure_non_fatal():
    """notify_fn 抛异常 → 不炸(通知 best-effort,事件已落)。"""
    logged = []

    def boom(title, msg):
        raise RuntimeError("plyer 不可用")

    escalate_stuck(
        story_key="STUCK-3",
        stage="verify",
        adapter="opencode",
        detection={"rule": "no_output_timeout", "reason": "idle 400s", "duration": 400},
        log_event_fn=lambda *a, **k: logged.append(k),
        notify_fn=boom,
    )
    # 事件仍落了(notify 抛异常没炸 escalate)
    assert len(logged) == 1
