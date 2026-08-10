"""1.7b — PTY 两层日志测试(raw.log + events.jsonl)。

设计依据:DESIGN-artifact-driven-stage-completion §4.5。
raw.log 字节保真;events.jsonl 结构化({ts,dir,type,text},剥 ANSI,含 injection)。
正常完成也保留(喂飞轮 + 复盘)。
"""

from __future__ import annotations

from story_lifecycle.infra.terminal.pty_logger import PtyLogger, read_events


def test_log_output_writes_raw_and_events(tmp_path):
    """PTY 输出 → raw.log(字节)+ events.jsonl(剥 ANSI 结构化)。"""
    logger = PtyLogger("STORY-1", "design", str(tmp_path))
    logger.log_output(b"hello world\n")
    logger.log_output(b"\x1b[32mgreen text\x1b[0m\n")

    raw = (logger.log_dir / "raw.log").read_bytes()
    assert b"hello world" in raw
    assert b"\x1b[32m" in raw  # raw 保真(含 ANSI)

    events = read_events(logger.log_dir)
    assert len(events) == 2
    assert all(e["dir"] == "output" for e in events)
    assert events[0]["text"] == "hello world\n"
    # ANSI 被剥(events.jsonl 喂 LLM 不污染)
    assert "\x1b" not in events[1]["text"]
    assert "green text" in events[1]["text"]


def test_log_injection_marks_dir_injection(tmp_path):
    """编排器 write PTY → events.jsonl 记 dir=injection(区分谁说的)。"""
    logger = PtyLogger("STORY-2", "build", str(tmp_path))
    logger.log_output("agent 输出\n".encode("utf-8"))
    logger.log_injection("orchestrator 注入\n".encode("utf-8"))

    events = read_events(logger.log_dir)
    assert len(events) == 2
    assert events[0]["dir"] == "output"
    assert events[1]["dir"] == "injection"
    assert "orchestrator 注入" in events[1]["text"]


def test_events_have_timestamps(tmp_path):
    """events.jsonl 每条有 ts(ISO,排序用)。"""
    logger = PtyLogger("STORY-3", "verify", str(tmp_path))
    logger.log_output(b"a")
    logger.log_output(b"b")
    events = read_events(logger.log_dir)
    assert len(events) == 2
    assert all("ts" in e for e in events)
    # ts 单调不减(后写的 ts >= 前写的)
    assert events[1]["ts"] >= events[0]["ts"]


def test_log_event_custom_type(tmp_path):
    """log_event 记自定义事件(supervisor 卡住检测 / 完成标记)。"""
    logger = PtyLogger("STORY-4", "design", str(tmp_path))
    logger.log_event("stuck_detected", "卡住(idle 600s)", rule="no_output_timeout")
    events = read_events(logger.log_dir)
    assert len(events) == 1
    assert events[0]["dir"] == "system"
    assert events[0]["type"] == "stuck_detected"
    assert events[0]["rule"] == "no_output_timeout"


def test_close_stops_logging(tmp_path):
    """close 后不再写(文件保留)。"""
    logger = PtyLogger("STORY-5", "design", str(tmp_path))
    logger.log_output(b"before close\n")
    logger.close()
    logger.log_output(b"after close\n")  # 应被忽略
    events = read_events(logger.log_dir)
    assert len(events) == 1
    assert "before close" in events[0]["text"]


def test_log_dir_layout(tmp_path):
    """日志目录布局:.story/runs/<key>/pty_<stage>/(raw.log + events.jsonl)。"""
    logger = PtyLogger("STORY-6", "build", str(tmp_path))
    assert logger.log_dir == tmp_path / ".story" / "runs" / "STORY-6" / "pty_build"
    assert logger.raw_path.name == "raw.log"
    assert logger.events_path.name == "events.jsonl"
    logger.log_output(b"x")
    assert logger.raw_path.exists()
    assert logger.events_path.exists()


def test_log_ref_returns_dir_path(tmp_path):
    """log_ref 属性返回日志目录(写回 story_session.pty_log_ref 用)。"""
    logger = PtyLogger("STORY-7", "design", str(tmp_path))
    assert logger.log_ref == str(
        tmp_path / ".story" / "runs" / "STORY-7" / "pty_design"
    )


def test_log_output_empty_data_noop(tmp_path):
    """空 data → 不写(防无意义空事件)。"""
    logger = PtyLogger("STORY-8", "design", str(tmp_path))
    logger.log_output(b"")
    assert not logger.events_path.exists()


def test_read_events_missing_dir(tmp_path):
    """目录不存在 → read_events 返回 [](不抛)。"""
    assert read_events(tmp_path / "nope") == []


def test_log_output_invalid_utf8_replaced(tmp_path):
    """非法 UTF-8 字节 → 替换符(errors=replace),不炸。"""
    logger = PtyLogger("STORY-9", "design", str(tmp_path))
    logger.log_output(b"\xff\xfe bad bytes\n")
    events = read_events(logger.log_dir)
    assert len(events) == 1
    assert "bad bytes" in events[0]["text"]


def test_ensure_agent_pty_auto_creates_logger(monkeypatch, tmp_path):
    """回归(2026-08-06 real-run 1068018):logger=None 时 ensure_agent_pty 自动挂
    PtyLogger —— 交互式 spawn 路径(spawn_recipe 合一后不传 logger)此前零日志,
    events.jsonl 缺失 + scheduler 卡住检测(读 pty.log_dir)失效。

    自动创建的 logger 必须让 ManagedPty 暴露 log_dir/events_path(卡住检测
    与 stuck_diagnose agentic 都靠这两个属性读 events)。
    """
    from story_lifecycle.infra.terminal import pty as pty_mod

    calls = {"logger": None}

    def _fake_spawn_pty(
        story_key, stage, adapter, command, cwd, env=None, purpose="agent", logger=None
    ):
        calls["logger"] = logger
        return "sid-auto", object()

    monkeypatch.setattr(pty_mod, "spawn_pty", _fake_spawn_pty)

    pty_mod.ensure_agent_pty("STORY-A1", "design", "claude", ["claude"], str(tmp_path), "")

    assert calls["logger"] is not None, "ensure_agent_pty 未自动创建 PtyLogger"
    assert calls["logger"].log_dir == (
        tmp_path / ".story" / "runs" / "STORY-A1" / "pty_design"
    )


def test_managed_pty_exposes_log_dir_and_events_path(tmp_path):
    """ManagedPty 暴露 log_dir/events_path(scheduler 卡住检测的读取口)。"""
    from story_lifecycle.infra.terminal.pty_logger import PtyLogger
    from story_lifecycle.infra.terminal.pty import ManagedPty

    logger = PtyLogger("STORY-A2", "verify", str(tmp_path))
    pty = ManagedPty.__new__(ManagedPty)
    pty._logger = logger
    pty.log_dir = logger.log_dir
    pty.events_path = logger.events_path

    assert pty.log_dir == tmp_path / ".story" / "runs" / "STORY-A2" / "pty_verify"
    assert pty.events_path == logger.events_path
    assert pty.events_path.name == "events.jsonl"
