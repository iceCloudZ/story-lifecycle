"""Tests for consult_runner.run_consult_sync(§5.5 / 实施步骤 2)。

纯 Handler 层测试 —— 注入 fake popen_fn / sleep_fn / kill_fn,零实时延迟、零真
spawn、零真 CLI。覆盖 DESIGN §8.1 列的所有 status 路径:
ok / timeout / spawn_failed / no_headless。

并验证三个关键设计约束:
- stdout/stderr 落 .log 文件(防 PIPE 死锁回归)
- 外援 env 注入 STORY_CONSULT_DEPTH=1(递归守卫)
- 失败不抛异常,返 ``{"status": ..., "error": ...}``
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from story_lifecycle.orchestrator.engine.consult_runner import (
    _build_reviewer_prompt,
    run_consult_sync,
)


# ── helpers: fake subprocess machinery ──────────────────────────────────


class _FakeProc:
    """Minimal Popen-like object: only poll() / stdin / pid used by runner."""

    def __init__(self, *, exits_immediately: bool = False, exit_code: int = 0):
        self.pid = 99999
        self._exits_immediately = exits_immediately
        self._exit_code = exit_code
        self._poll_calls = 0
        # stdin is a simple writable object; runner writes prompt then closes.
        self.stdin = _FakeStdin()
        self._killed = False

    def poll(self):
        # 0 = still running, None → return None
        if self._exits_immediately:
            return self._exit_code
        # Stay alive until caller mutates state (test writes result file)
        # The default behavior: report not exited so the poll loop keeps going.
        self._poll_calls += 1
        return None


class _FakeStdin:
    def __init__(self):
        self.written = b""
        self.closed = False

    def write(self, data: bytes):
        self.written += data
        return len(data)

    def close(self):
        self.closed = True


def _make_popen_fn(proc: _FakeProc, *, raise_on_first_n: int = 0):
    """Build a fake Popen that returns ``proc``; optionally raise first N times.

    raise_on_first_n: simulate spawn failures (triggers retry logic).
    """
    state = {"calls": 0, "raises_left": raise_on_first_n}

    def fake_popen(cmd, **kwargs):
        state["calls"] += 1
        if state["raises_left"] > 0:
            state["raises_left"] -= 1
            raise OSError(f"simulated spawn failure #{state['calls']}")
        # Capture kwargs for assertions (env / cwd / stdout / stderr)
        fake_popen.last_kwargs = kwargs  # type: ignore[attr-defined]
        return proc

    fake_popen.state = state  # type: ignore[attr-defined]
    return fake_popen


# ── TestConsultRunnerPrompt ────────────────────────────────────────────


class TestReviewerPrompt:
    def test_prompt_embeds_focus_and_result_file(self):
        p = _build_reviewer_prompt(focus="check X", result_file=".story/consult/r1.json")
        assert "check X" in p
        assert ".story/consult/r1.json" in p
        # 关键纪律文案
        assert "advisory" in p
        assert "不写业务代码" in p or "不写业务代码" in p
        assert "story consult" in p  # 递归守卫提示

    def test_prompt_format_uses_curly_escape(self):
        """prompt 模板里的 {{ }} 不能泄露到成品(f-string 双花括号转义)。"""
        p = _build_reviewer_prompt(focus="x", result_file="r")
        assert "{{" not in p
        assert "}}" not in p


# ── TestRunConsultSyncStatusPaths ──────────────────────────────────────


@pytest.fixture
def workspace(tmp_path: Path) -> str:
    return str(tmp_path)


class TestRunConsultSyncOk:
    def test_ok_reads_findings_when_result_file_appears(self, workspace, tmp_path):
        """外援写结果文件 → status=ok,findings 来自 robust_json_parse。"""
        rid = "oktest12345"
        # 把结果文件放到 fake_proc 第一次 poll 后写
        proc = _FakeProc()

        result_data = {
            "summary": "all good",
            "findings": ["f1"],
            "recommendation": "go",
            "confidence": "high",
        }
        popen_fn = _make_popen_fn(proc)

        # patch the poll method to write the result file after first poll
        original_poll = proc.poll

        def poll_then_write():
            r = original_poll()
            # Write result file once (idempotent via marker)
            if not getattr(proc, "_wrote", False):
                rp = Path(workspace) / ".story" / "consult" / f"{rid}.json"
                rp.write_text(json.dumps(result_data), encoding="utf-8")
                proc._wrote = True  # type: ignore[attr-defined]
            return r

        proc.poll = poll_then_write  # type: ignore[assignment]

        result = run_consult_sync(
            adapter_name="claude",
            focus="investigate X",
            workspace=workspace,
            request_id=rid,
            popen_fn=popen_fn,
            sleep_fn=lambda _s: None,  # 零延迟
            kill_fn=lambda _p: None,
            poll_interval=0.01,
            timeout=10,
        )

        assert result["status"] == "ok"
        assert result["error"] == ""
        assert result["findings"] == result_data

    def test_ok_finds_log_file_written_alongside(self, workspace):
        """stdout/stderr 落 .log(防 PIPE 死锁回归 —— planner.py 已知坑)。"""
        rid = "logtest12345"
        proc = _FakeProc()
        popen_fn = _make_popen_fn(proc)
        original_poll = proc.poll

        def poll_then_write():
            r = original_poll()
            if not getattr(proc, "_wrote", False):
                rp = Path(workspace) / ".story" / "consult" / f"{rid}.json"
                rp.write_text('{"summary": "ok"}', encoding="utf-8")
                proc._wrote = True  # type: ignore[attr-defined]
            return r

        proc.poll = poll_then_write  # type: ignore[assignment]

        run_consult_sync(
            adapter_name="claude",
            focus="x",
            workspace=workspace,
            request_id=rid,
            popen_fn=popen_fn,
            sleep_fn=lambda _s: None,
            kill_fn=lambda _p: None,
            poll_interval=0.01,
            timeout=10,
        )

        log_path = Path(workspace) / ".story" / "consult" / f"{rid}.log"
        assert log_path.exists(), (
            "stdout/stderr must drain to .log file, never PIPE (防死锁回归)"
        )

    def test_env_injects_recursion_depth_guard(self, workspace):
        """外援 env 必须注入 STORY_CONSULT_DEPTH=1(递归守卫)。"""
        rid = "envtest12345"
        proc = _FakeProc()
        popen_fn = _make_popen_fn(proc)
        original_poll = proc.poll

        def poll_then_write():
            r = original_poll()
            if not getattr(proc, "_wrote", False):
                rp = Path(workspace) / ".story" / "consult" / f"{rid}.json"
                rp.write_text('{"summary": "ok"}', encoding="utf-8")
                proc._wrote = True  # type: ignore[attr-defined]
            return r

        proc.poll = poll_then_write  # type: ignore[assignment]

        run_consult_sync(
            adapter_name="claude",
            focus="x",
            workspace=workspace,
            request_id=rid,
            env={"PATH": "/bin"},  # 故意不含 DEPTH —— runner 应强制注入
            popen_fn=popen_fn,
            sleep_fn=lambda _s: None,
            kill_fn=lambda _p: None,
            poll_interval=0.01,
            timeout=10,
        )

        env_used = popen_fn.last_kwargs["env"]
        assert env_used.get("STORY_CONSULT_DEPTH") == "1", (
            "reviewer env must force STORY_CONSULT_DEPTH=1 (递归守卫)"
        )

    def test_ok_overwrites_preexisting_result_file(self, workspace):
        """若上次的 .json 残留,runner 启动时应清掉(防误读旧结果)。"""
        rid = "cleartest123"
        rp = Path(workspace) / ".story" / "consult" / f"{rid}.json"
        rp.parent.mkdir(parents=True, exist_ok=True)
        rp.write_text('{"old": "STALE"}', encoding="utf-8")

        proc = _FakeProc()
        popen_fn = _make_popen_fn(proc)
        original_poll = proc.poll

        def poll_then_write():
            r = original_poll()
            if not getattr(proc, "_wrote", False):
                rp.write_text('{"summary": "fresh"}', encoding="utf-8")
                proc._wrote = True  # type: ignore[attr-defined]
            return r

        proc.poll = poll_then_write  # type: ignore[assignment]

        result = run_consult_sync(
            adapter_name="claude",
            focus="x",
            workspace=workspace,
            request_id=rid,
            popen_fn=popen_fn,
            sleep_fn=lambda _s: None,
            kill_fn=lambda _p: None,
            poll_interval=0.01,
            timeout=10,
        )

        assert result["status"] == "ok"
        assert result["findings"] == {"summary": "fresh"}
        assert "old" not in result["findings"]


class TestRunConsultSyncFailures:
    def test_no_headless_when_adapter_missing(self, workspace):
        """codex 无 headless_launch_cmd(继承 base return None)→ status=no_headless。"""
        result = run_consult_sync(
            adapter_name="codex",  # codex 没实现 headless(DESIGN §3.6)
            focus="x",
            workspace=workspace,
            request_id="codextest12",
        )
        assert result["status"] == "no_headless"
        assert "codex" in result["error"]
        assert result["findings"] == {}

    def test_no_headless_when_adapter_unknown(self, workspace):
        """未知 adapter → status=no_headless,不抛异常。"""
        result = run_consult_sync(
            adapter_name="totally-fake-adapter",
            focus="x",
            workspace=workspace,
            request_id="badtest12345",
        )
        assert result["status"] == "no_headless"
        assert "error" in result and result["error"]

    def test_timeout_when_reviewer_never_writes(self, workspace):
        """外援始终不写结果文件 → status=timeout(不抛,不卡死)。"""
        proc = _FakeProc()  # 永不退出,永不写文件
        popen_fn = _make_popen_fn(proc)

        # 用很短的 timeout + zero sleep 让循环跑几次
        result = run_consult_sync(
            adapter_name="claude",
            focus="x",
            workspace=workspace,
            request_id="timetest1234",
            popen_fn=popen_fn,
            sleep_fn=lambda _s: None,
            kill_fn=lambda _p: None,
            poll_interval=0.01,
            timeout=0.05,  # 立刻超时
        )
        assert result["status"] == "timeout"
        assert "0.05" in result["error"]
        assert result["findings"] == {}

    def test_spawn_failed_when_popen_always_raises(self, workspace):
        """Popen 连续失败超 max_attempts → status=spawn_failed。"""
        proc = _FakeProc()
        popen_fn = _make_popen_fn(proc, raise_on_first_n=99)  # 永远 raise

        result = run_consult_sync(
            adapter_name="claude",
            focus="x",
            workspace=workspace,
            request_id="popenfail123",
            popen_fn=popen_fn,
            sleep_fn=lambda _s: None,
            kill_fn=lambda _p: None,
            poll_interval=0.01,
            timeout=10,
            max_attempts=3,
        )
        assert result["status"] == "spawn_failed"
        assert "Popen failed" in result["error"]
        assert popen_fn.state["calls"] == 3

    def test_spawn_failed_when_reviewer_exits_without_writing(self, workspace):
        """外援立刻退出且没写结果文件 → 重试 max_attempts 后 spawn_failed。"""
        proc = _FakeProc(exits_immediately=True, exit_code=0)
        popen_fn = _make_popen_fn(proc)

        result = run_consult_sync(
            adapter_name="claude",
            focus="x",
            workspace=workspace,
            request_id="exitnoout123",
            popen_fn=popen_fn,
            sleep_fn=lambda _s: None,
            kill_fn=lambda _p: None,
            poll_interval=0.01,
            timeout=10,
            max_attempts=2,
        )
        assert result["status"] == "spawn_failed"
        assert "exited without writing" in result["error"]


class TestRunConsultSyncStdinInjection:
    def test_prompt_written_to_proc_stdin_and_closed(self, workspace):
        """prompt 必须写进外援 stdin 然后关闭(同 planner spawn 协议)。"""
        rid = "stdintest123"
        proc = _FakeProc()
        popen_fn = _make_popen_fn(proc)
        original_poll = proc.poll

        def poll_then_write():
            r = original_poll()
            if not getattr(proc, "_wrote", False):
                rp = Path(workspace) / ".story" / "consult" / f"{rid}.json"
                rp.write_text('{"summary": "ok"}', encoding="utf-8")
                proc._wrote = True  # type: ignore[attr-defined]
            return r

        proc.poll = poll_then_write  # type: ignore[assignment]

        run_consult_sync(
            adapter_name="claude",
            focus="MY UNIQUE FOCUS",
            workspace=workspace,
            request_id=rid,
            popen_fn=popen_fn,
            sleep_fn=lambda _s: None,
            kill_fn=lambda _p: None,
            poll_interval=0.01,
            timeout=10,
        )

        assert proc.stdin.closed, "stdin must be closed after writing prompt"
        assert b"MY UNIQUE FOCUS" in proc.stdin.written


class TestRunConsultSyncNoRealSpawn:
    """测试缝隔离:default popen_fn 是真 subprocess.Popen,但所有上面的测试都注入了 fake。

    本类验证:不注入 popen_fn 时,**不会**因调 get_adapter(未注册名)就抛异常
    (no_headless 路径在 Popen 之前就 return 了)。这是 design 的"不抛异常"承诺。
    """

    def test_no_headless_path_does_not_spawn(self, workspace):
        """no_headless 路径在 Popen 之前就返回 → 不需要注入 popen_fn。"""
        # mock_patch.get_adapter to avoid real claude CLI lookup
        from unittest import mock

        with mock.patch(
            "story_lifecycle.orchestrator.engine.consult_runner.get_adapter"
        ) as m:
            m.side_effect = ValueError("simulated unknown")
            result = run_consult_sync(
                adapter_name="claude",
                focus="x",
                workspace=workspace,
                request_id="nosetup12345",
            )
        assert result["status"] == "no_headless"
        assert "simulated unknown" in result["error"]


if __name__ == "__main__":
    # 允许直接 python this_file.py 跑(不依赖 pytest)
    import subprocess as _sp  # noqa: F401

    pytest.main([__file__, "-v"])
