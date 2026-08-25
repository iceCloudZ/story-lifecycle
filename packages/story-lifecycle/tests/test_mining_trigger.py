"""M2 挖掘触发器单测 — 防抖/单飞/软 seam（DESIGN-knowledge-flywheel-closure §4.2）。

约定：monkeypatch ``_launch_worker``（不起真线程）、``_now``（时间旅行）、
``_miner_available``（模拟 miner 缺失）——全部走模块自留的注入点。
"""

from pathlib import Path

from story_lifecycle.orchestrator.learning import mining_trigger


def setup_function():
    mining_trigger._reset_state()


def test_noop_when_miner_missing(monkeypatch):
    """软 seam：miner 不可导入 → no-op，不计数不留状态。"""
    monkeypatch.setattr(mining_trigger, "_miner_available", lambda: False)
    assert mining_trigger.maybe_trigger_incremental("tapd-1") is False
    assert mining_trigger._completed_since_run == 0


def test_first_completion_triggers_immediately(monkeypatch):
    """从未跑过 → interval 条件天然满足，首个完成即真跑。"""
    launched = []
    monkeypatch.setattr(mining_trigger, "_launch_worker", lambda sk: launched.append(sk))
    assert mining_trigger.maybe_trigger_incremental("tapd-1") is True
    assert launched == ["tapd-1"]


def test_debounce_interval(monkeypatch):
    """间隔内 + 计数不足 → 不跑；时间推进 31min → 跑。"""
    launched = []
    monkeypatch.setattr(mining_trigger, "_launch_worker", lambda sk: launched.append(sk))
    now = 1_000_000.0
    monkeypatch.setattr(mining_trigger, "_now", lambda: now)

    assert mining_trigger.maybe_trigger_incremental("tapd-1") is True  # 首跑
    mining_trigger._worker_finished()
    assert mining_trigger.maybe_trigger_incremental("tapd-2") is False  # +1 完成，间隔内
    assert mining_trigger.maybe_trigger_incremental("tapd-3") is False  # +2 完成，仍不足 3
    monkeypatch.setattr(mining_trigger, "_now", lambda: now + 31 * 60)
    assert mining_trigger.maybe_trigger_incremental("tapd-4") is True
    assert len(launched) == 2


def test_debounce_count(monkeypatch):
    """间隔未到，但累计第 3 个完成 → 计数条件满足 → 跑。"""
    launched = []
    monkeypatch.setattr(mining_trigger, "_launch_worker", lambda sk: launched.append(sk))
    monkeypatch.setattr(mining_trigger, "_now", lambda: 1_000_000.0)

    assert mining_trigger.maybe_trigger_incremental("tapd-1") is True  # 首跑
    mining_trigger._worker_finished()
    assert mining_trigger.maybe_trigger_incremental("tapd-2") is False  # 计数 1
    assert mining_trigger.maybe_trigger_incremental("tapd-3") is False  # 计数 2
    assert mining_trigger.maybe_trigger_incremental("tapd-4") is True  # 计数 3 → 触发
    assert len(launched) == 2


def test_single_flight_skips_while_running(monkeypatch):
    """单飞：worker 在跑时即使时间/计数条件都满足也不重复起。"""
    launched = []
    monkeypatch.setattr(mining_trigger, "_launch_worker", lambda sk: launched.append(sk))
    now = 1_000_000.0
    monkeypatch.setattr(mining_trigger, "_now", lambda: now)

    assert mining_trigger.maybe_trigger_incremental("tapd-1") is True
    # 未 _worker_finished：时间推进 2h + 计数堆上 → 仍不跑（worker 未死）
    monkeypatch.setattr(mining_trigger, "_now", lambda: now + 2 * 60 * 60)
    assert mining_trigger.maybe_trigger_incremental("tapd-2") is False
    assert mining_trigger.maybe_trigger_incremental("tapd-3") is False
    assert len(launched) == 1
    # worker 结束 → 下一个完成立刻满足 interval 条件 → 跑
    mining_trigger._worker_finished()
    assert mining_trigger.maybe_trigger_incremental("tapd-4") is True
    assert len(launched) == 2


def test_run_pipeline_step_failure_is_soft(monkeypatch):
    """子步骤非零退出：不抛异常、顺序停止、单飞标记复位（story 完成路径无感）。"""

    class FakeProc:
        returncode = 1
        stderr = "boom"
        stdout = ""

    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd[1:])
        return FakeProc()

    monkeypatch.setattr(mining_trigger, "_miner_root", lambda: Path("D:/fake/miner-root"))
    monkeypatch.setattr(mining_trigger.subprocess, "run", fake_run)

    mining_trigger._run_pipeline("tapd-x")  # 不抛
    assert calls == [["-m", "miner.story_ingest"]]  # 首步失败即停，不跑 link/playbooks
    assert mining_trigger._worker_alive is False


def test_run_pipeline_miner_root_unresolved(monkeypatch):
    """miner root 解析失败 → warning 即止，不影响状态。"""
    monkeypatch.setattr(mining_trigger, "_miner_root", lambda: None)
    mining_trigger._run_pipeline("tapd-x")  # 不抛
    assert mining_trigger._worker_alive is False
