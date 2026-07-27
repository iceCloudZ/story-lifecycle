"""PTY 两层日志 — raw.log(字节保真)+ events.jsonl(结构化,剥 ANSI)。

DESIGN-artifact-driven-stage-completion §4.5 / STEP 1 子任务 1.7b。

业界一致选结构化 JSONL(Claude Code 原生 jsonl / Agent Logging 101)。本设计两层:
  - raw.log:原始字节保真(回放/调试用)
  - events.jsonl:{ts, dir(output|injection), type, text, tool_call?}
    - 剥 ANSI(喂 LLM/飞轮,不污染)
    - 含编排器注入记录(dir=injection,write PTY 时记一条)
    - 正常完成也保留(喂飞轮 + 复盘)

日志目录:.story/runs/<key>/pty_<stage>/  (raw.log + events.jsonl)

**线程安全**:_read_loop 是 daemon 线程,write 在主线程 —— 两者都可能并发写日志。
用一个 threading.Lock 串行化文件追加(日志量不大,锁开销可忽略)。

best-effort:任何 IO 异常不抛(日志失败不能炸 PTY 主流程)。
"""

from __future__ import annotations

import json
import logging
import re
import threading
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger("story-lifecycle.pty_logger")

# ANSI 转义序列(CSI / OSC / 字符集切换 / 单字符 shift)。复用 awaiting_detector 的模式。
_ANSI_RE = re.compile(
    r"\x1b\[[0-9;?]*[A-Za-z]|\x1b\][^\x07]*\x07|\x1b[()][AB012]|\x1b[=>]"
)


def _strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


def _now_iso() -> str:
    """UTC ISO 时间戳(毫秒精度,排序用)。"""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


class PtyLogger:
    """两层 PTY 日志(raw + events.jsonl),绑定一个 story/stage。

    ManagedPty 持有一个可选 PtyLogger:_distribute 时 log_output,write 时 log_injection。
    正常完成也 flush 保留(不删)。
    """

    def __init__(self, story_key: str, stage: str, workspace: str):
        self.story_key = story_key
        self.stage = stage
        self.workspace = workspace
        # 日志目录:.story/runs/<key>/pty_<stage>/
        self.log_dir = Path(workspace) / ".story" / "runs" / story_key / f"pty_{stage}"
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.raw_path = self.log_dir / "raw.log"
        self.events_path = self.log_dir / "events.jsonl"
        self._lock = threading.Lock()
        self._closed = False

    def _decode(self, data: bytes) -> str:
        """bytes → str(UTF-8,替换非法字节)。"""
        try:
            return data.decode("utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            return repr(data)

    def log_output(self, data: bytes) -> None:
        """记一条 PTY 输出(code agent → 终端)。raw 字节 + 剥 ANSI 的 events.jsonl。"""
        if self._closed or not data:
            return
        try:
            text = self._decode(data)
            with self._lock:
                # raw.log:字节保真追加
                with open(self.raw_path, "ab") as fh:
                    fh.write(data)
                # events.jsonl:结构化,剥 ANSI
                clean = _strip_ansi(text)
                event = {
                    "ts": _now_iso(),
                    "dir": "output",
                    "type": "text",
                    "text": clean,
                }
                with open(self.events_path, "a", encoding="utf-8") as fh:
                    fh.write(json.dumps(event, ensure_ascii=False) + "\n")
        except OSError as exc:
            log.debug("pty log_output failed (non-fatal): %s", exc)

    def log_injection(self, data: bytes) -> None:
        """记一条编排器注入(主线程 write → PTY)。dir=injection,让飞轮分清谁说的。"""
        if self._closed or not data:
            return
        try:
            text = self._decode(data)
            with self._lock:
                clean = _strip_ansi(text)
                event = {
                    "ts": _now_iso(),
                    "dir": "injection",
                    "type": "text",
                    "text": clean,
                }
                with open(self.events_path, "a", encoding="utf-8") as fh:
                    fh.write(json.dumps(event, ensure_ascii=False) + "\n")
        except OSError as exc:
            log.debug("pty log_injection failed (non-fatal): %s", exc)

    def log_event(self, event_type: str, text: str, **extra) -> None:
        """记一条自定义结构化事件(如 supervisor 检测到卡住 / 完成)。"""
        if self._closed:
            return
        try:
            with self._lock:
                event = {
                    "ts": _now_iso(),
                    "dir": "system",
                    "type": event_type,
                    "text": text,
                    **extra,
                }
                with open(self.events_path, "a", encoding="utf-8") as fh:
                    fh.write(json.dumps(event, ensure_ascii=False) + "\n")
        except OSError as exc:
            log.debug("pty log_event failed (non-fatal): %s", exc)

    def close(self) -> None:
        """标记关闭(不再写)。文件保留(喂飞轮 + 复盘)。"""
        self._closed = True

    @property
    def log_ref(self) -> str:
        """返回日志目录路径(写回 story_session.pty_log_ref 用)。"""
        return str(self.log_dir)


def read_events(log_dir: str | Path, *, limit: int = 0) -> list[dict]:
    """读 events.jsonl(辅助:supervisor 卡住检测 / 测试断言)。limit=0 读全部。"""
    events_path = Path(log_dir) / "events.jsonl"
    if not events_path.exists():
        return []
    events = []
    try:
        with open(events_path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    events.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except OSError:
        return []
    if limit > 0:
        return events[-limit:]
    return events
