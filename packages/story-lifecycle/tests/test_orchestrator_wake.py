"""PLAN-dsh-absorption A4:编排线程 wake() 低延迟唤醒(事件驱动最小形态)。

红线测试:
- 不新增第二条调度路径 —— wake 只让同一次 tick 提前发生(线程级验证)。
- 停机响应:run() 等在 _wake_event 上,stop() 必须把它一并 set,
  否则停机要等满一个 poll_interval(回归守护)。
- 三个 wake 点 wiring:judge 完成 / PTY 会话结束 / headless EOF。
"""

from __future__ import annotations

import asyncio
import io
import threading
import time
from types import SimpleNamespace

import pytest

from story_lifecycle.orchestrator.engine import wake as wake_mod
from story_lifecycle.orchestrator.engine.claude_stream import supervise_headless_stdout
from story_lifecycle.orchestrator.engine.supervisor import supervise_pty_session
from story_lifecycle.orchestrator.scheduler import OrchestratorThread


@pytest.fixture(autouse=True)
def _clean_registry():
    """每测前后清注册表,避免线程测试间串扰。"""
    wake_mod.register(None)
    yield
    wake_mod.register(None)


class TestWakeRegistry:
    def test_wake_without_register_is_noop(self):
        """未注册时 wake() 静默 no-op(测试/CLI 无 serve 场景不炸)。"""
        wake_mod.wake()  # 不抛即过

    def test_register_then_wake_sets_event(self):
        ev = threading.Event()
        wake_mod.register(ev)
        try:
            wake_mod.wake()
            assert ev.is_set()
        finally:
            wake_mod.register(None)

    def test_register_none_unregisters(self):
        ev = threading.Event()
        wake_mod.register(ev)
        wake_mod.register(None)
        wake_mod.wake()
        assert not ev.is_set()


class TestOrchestratorThreadWake:
    def test_wake_triggers_immediate_tick(self, monkeypatch):
        """poll_interval=10s 下,wake() 让下一轮 tick 在 <<10s 内发生。

        事件驱动吸收的核心语义:同一次 tick 提前,不是另一条调度路径。
        """
        ticks: list[float] = []
        th = OrchestratorThread(poll_interval=10.0)
        monkeypatch.setattr(th, "_tick", lambda: ticks.append(time.monotonic()))
        th.start()
        try:
            deadline = time.monotonic() + 2.0
            while not ticks and time.monotonic() < deadline:
                time.sleep(0.02)
            assert len(ticks) == 1  # 启动首轮
            wake_mod.wake()
            deadline = time.monotonic() + 2.0
            while len(ticks) < 2 and time.monotonic() < deadline:
                time.sleep(0.02)
        finally:
            th.stop()
            th.join(timeout=3)
        assert len(ticks) >= 2  # 本来要等 10s,wake 后 2s 内 tick 了
        assert not th.is_alive()

    def test_stop_exits_promptly_despite_long_poll(self):
        """停机响应回归:stop() 必须同时 set _wake_event,否则等满 poll_interval。"""
        th = OrchestratorThread(poll_interval=30.0)
        th.start()
        time.sleep(0.2)
        t0 = time.monotonic()
        th.stop()
        th.join(timeout=3)
        assert not th.is_alive()
        assert time.monotonic() - t0 < 2.5  # 秒停,不是被 join 超时拖着


class TestWakePointsWiring:
    @pytest.mark.asyncio
    async def test_pty_session_end_calls_wake(self, monkeypatch):
        """PTY 会话结束(sentinel 退出)→ finally 里 wake 被调(PTY 死亡→tick ~0s)。"""
        calls: list[int] = []
        monkeypatch.setattr(
            "story_lifecycle.orchestrator.engine.supervisor.wake",
            lambda: calls.append(1),
        )
        tap = asyncio.Queue()
        tap.put_nowait(None)  # sentinel → 立即退出
        fake_pty = SimpleNamespace(
            add_tap=lambda maxsize=512: tap,
            remove_tap=lambda t: None,
            write=lambda d: None,
            alive=True,
        )
        await supervise_pty_session(
            pty=fake_pty,
            adapter="codex",
            story_facts={"story_key": "S-W", "stage": "implement"},
            is_awaiting_fn=lambda b: None,
            llm_invoke=lambda p: "{}",
            log_event_fn=lambda *a, **k: None,
        )
        assert calls, "会话结束必须唤醒编排线程"

    def test_headless_eof_calls_wake(self, monkeypatch):
        """headless stdout EOF(proc 退出)→ 返回前 wake 被调。"""
        calls: list[int] = []
        monkeypatch.setattr(
            "story_lifecycle.orchestrator.engine.claude_stream.wake",
            lambda: calls.append(1),
        )
        fake_proc = SimpleNamespace(stdout=io.BytesIO(b""), stderr=None)  # 立即 EOF
        decisions = supervise_headless_stdout(
            proc=fake_proc,
            adapter="kimi",
            story_facts={"story_key": "S-W2", "stage": "build"},
            llm_invoke=lambda p: "{}",
            log_event_fn=lambda *a, **k: None,
        )
        assert decisions == []
        assert calls, "headless EOF 必须唤醒编排线程"

    def test_judge_task_completion_calls_wake(self, monkeypatch):
        """judge 完成(含异常 fallback approve)→ 结果落地后 wake 被调。

        _judge_task 内的 wake 是函数级 import(每次调用从 wake 模块重新解析),
        故 patch wake_mod.wake 即可生效。
        """
        from story_lifecycle.orchestrator.evaluation import stage_completion

        calls: list[int] = []
        monkeypatch.setattr(wake_mod, "wake", lambda: calls.append(1))
        monkeypatch.setattr(
            stage_completion,
            "judge_stage_completion",
            lambda req: (_ for _ in ()).throw(RuntimeError("force fallback")),
        )
        th = OrchestratorThread(poll_interval=1.0)
        story = {"story_key": "S-J", "workspace": "", "context_json": "{}"}
        th._judge_task("S-J", "design", {}, {}, story)
        assert calls, "judge 完成(含 fallback)必须唤醒编排线程"
