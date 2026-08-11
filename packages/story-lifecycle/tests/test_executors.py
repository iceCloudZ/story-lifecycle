"""设计 13 Step 3：StageExecutor 测试（executors.py）。

覆盖：
- InteractiveStageExecutor: get_pty 未 spawn 返回 None / is_artifacts_ready 判落地 /
  maybe_spawn 不 spawn（半自动）
- AutomaticStageExecutor: maybe_spawn 自动 spawn（全自动）
- get_pty_for_stage 按 (story, stage) 查 PTY 注册表
"""

import json
from pathlib import Path

import pytest

from story_lifecycle.infra.db import models as db
from story_lifecycle.orchestrator.executors import (
    AutomaticStageExecutor,
    BaseStageExecutor,
    InteractiveStageExecutor,
    make_stage_executor,
)
from story_lifecycle.orchestrator.abc import StageExecutor


@pytest.fixture(autouse=True)
def _isolated_evidence_root(tmp_path, monkeypatch):
    """把 evidence 根锁到 workspace/story（不走向上找 .agents 的真实路径）。

    story_evidence_root 会沿父目录找 .agents/AGENTS.md —— 测试 tmp_path 位于
    C:/Users/zzh58 下,命中真实 .agents → evidence 候选指到用户 story 目录
    （被真实跑测污染,spec.md 存在 → is_artifacts_ready 误判 True）。
    """
    import story_lifecycle.infra.story_paths as sp

    monkeypatch.setattr(
        sp, "story_evidence_root", lambda ws: Path(str(ws or ".")) / "story"
    )


@pytest.fixture
def tmp_story(tmp_path, isolated_story_home):
    """创建一个临时 story（minimal profile, design stage, 有 _agent_actions）。"""
    ws = tmp_path / "ws"
    ws.mkdir()
    db.create_story("S-EXEC-1", "测试 executor", str(ws), profile="minimal")
    db.update_story(
        "S-EXEC-1",
        context_json=json.dumps(
            {
                "_plan_confirmed": True,
                "_agent_actions": [
                    {"action": "launch", "stage": "design", "adapter": "claude"},
                    {"action": "launch", "stage": "build", "adapter": "claude"},
                    {"action": "launch", "stage": "verify", "adapter": "claude"},
                ],
            },
            ensure_ascii=False,
        ),
    )
    return "S-EXEC-1"


@pytest.fixture
def tmp_story_with_spec(tmp_story):
    """在 tmp_story 基础上写 spec.md（模拟 design 产出）。"""
    ws = db.get_story(tmp_story)["workspace"]
    # minimal profile design stage artifacts → story/spec.md（evidence 目录）
    import story_lifecycle.infra.story_paths as sp

    story_dir = sp.story_evidence_dir(ws, tmp_story, db.get_story(tmp_story)["title"])
    story_dir.mkdir(parents=True, exist_ok=True)
    (story_dir / "spec.md").write_text("# 方案\n", encoding="utf-8")
    return tmp_story


class TestBaseStageExecutor:
    def test_is_stage_executor_abc_subclass(self):
        assert issubclass(InteractiveStageExecutor, StageExecutor)
        assert issubclass(AutomaticStageExecutor, StageExecutor)
        assert issubclass(BaseStageExecutor, StageExecutor)

    def test_make_executor_returns_interactive_for_minimal(self, tmp_story):
        """minimal profile → InteractiveStageExecutor（半自动）"""
        story = db.get_story(tmp_story)
        ex = make_stage_executor(story, json.loads(story["context_json"]))
        assert isinstance(ex, InteractiveStageExecutor)

    def test_make_executor_returns_automatic_for_realtest(self, tmp_story):
        """realtest profile → AutomaticStageExecutor（全自动）"""
        db.update_story(tmp_story, profile="realtest")
        story = db.get_story(tmp_story)
        ex = make_stage_executor(story, json.loads(story["context_json"]))
        assert isinstance(ex, AutomaticStageExecutor)


class TestInteractiveStageExecutor:
    def test_get_pty_returns_none_when_not_spawned(self, tmp_story):
        """没 spawn 时 get_pty 返回 None"""
        executor = InteractiveStageExecutor()
        assert executor.get_pty(tmp_story, "design") is None

    def test_is_artifacts_ready_false_when_no_spec(self, tmp_story):
        """spec.md 不存在时 False"""
        executor = InteractiveStageExecutor()
        assert executor.is_artifacts_ready(tmp_story, "design") is False

    def test_is_artifacts_ready_requires_declare_when_pty_alive(
        self, tmp_story_with_spec
    ):
        """PTY 活时只认 declare event —— 写了 spec.md 但没 declare 不算完成（1068018 事故修复）。"""
        executor = InteractiveStageExecutor()
        # spec.md 已写（tmp_story_with_spec fixture），但没 declare + PTY 活 → False
        assert (
            executor.is_artifacts_ready(tmp_story_with_spec, "design", pty_alive=True)
            is False
        )

    def test_is_artifacts_ready_true_when_declared(self, tmp_story_with_spec):
        """declare 后（event 落库）→ True（PTY 活/死都认 declare）。"""
        executor = InteractiveStageExecutor()
        db.log_event(
            tmp_story_with_spec,
            "design",
            "artifact_declared",
            {"doc_type": "spec", "version": 1, "summary": "ok", "files_changed": []},
        )
        assert (
            executor.is_artifacts_ready(tmp_story_with_spec, "design", pty_alive=True)
            is True
        )

    def test_is_artifacts_ready_no_file_fallback_even_when_pty_dead(
        self, tmp_story_with_spec
    ):
        """PTY 死 + 没 declare → 不文件兜底（砍文件兜底，防 spawn retry 间隙边写边判）。"""
        executor = InteractiveStageExecutor()
        # spec.md 已写，没 declare，PTY 死了 → 仍 False（只认 declare event）
        assert (
            executor.is_artifacts_ready(tmp_story_with_spec, "design", pty_alive=False)
            is False
        )

    def test_maybe_spawn_does_nothing_in_interactive(self, tmp_story):
        """半自动模式 maybe_spawn 不 spawn"""
        executor = InteractiveStageExecutor()
        ctx = json.loads(db.get_story(tmp_story)["context_json"])
        executor.maybe_spawn(tmp_story, "design", ctx)
        assert executor.get_pty(tmp_story, "design") is None


class TestAutomaticStageExecutor:
    def test_maybe_spawn_spawns_pty(self, tmp_story, monkeypatch):
        """全自动模式 maybe_spawn 起 PTY"""
        executor = AutomaticStageExecutor()

        spawned = {}

        class FakePty:
            alive = True
            session_id = "sess-1"

        def _fake_ensure(*a, **kw):
            spawned["called"] = True
            return ("sess-1", FakePty())

        import story_lifecycle.infra.terminal.pty as pty_mod

        monkeypatch.setattr(pty_mod, "ensure_agent_pty", _fake_ensure)
        # 模拟注册表命中（fake ensure 不写真实注册表）
        monkeypatch.setattr(
            pty_mod,
            "get_pty_for_stage",
            lambda story_id, stage, purpose="agent": (
                FakePty() if spawned.get("called") else None
            ),
        )

        class FakeAdapter:
            name = "claude"
            prespecified_session_id = True
            default_model = "sonnet"
            readiness_marker = None

            def start_session(self, *a, **kw):
                from story_lifecycle.knowledge.adapters.base import SessionSpec

                return SessionSpec(
                    command=["claude-fake"], pty_prompt="", readiness_marker=None
                )

            def write_anchor(self, **kw):
                return None

        import story_lifecycle.knowledge.adapters as adapters_mod

        monkeypatch.setattr(adapters_mod, "get_adapter", lambda n: FakeAdapter())
        # 避免真正起 PtyLogger / supervisor 线程
        import story_lifecycle.infra.terminal.pty_logger as plog_mod

        monkeypatch.setattr(
            plog_mod, "PtyLogger", lambda *a, **kw: type("L", (), {"log_ref": "x"})()
        )

        ctx = json.loads(db.get_story(tmp_story)["context_json"])
        executor.maybe_spawn(tmp_story, "design", ctx)
        assert spawned.get("called"), "maybe_spawn 应触发 spawn"
        pty = executor.get_pty(tmp_story, "design")
        assert pty is not None

    def test_maybe_spawn_noop_when_stage_completed(self, tmp_story, monkeypatch):
        """已完成 stage 不重复 spawn"""
        executor = AutomaticStageExecutor()
        story = db.get_story(tmp_story)
        ctx = json.loads(story["context_json"])
        ctx["_completed_stages"] = ["design"]
        db.update_story(tmp_story, context_json=json.dumps(ctx, ensure_ascii=False))
        executor.maybe_spawn(tmp_story, "design", ctx)
        assert executor.get_pty(tmp_story, "design") is None


# ---- kill_headless / cleanup_headless_all 回归(2026-08-11 headless 泄漏修复) ----


def test_kill_headless_pops_registry_and_kills(monkeypatch):
    """kill_headless 必须从 _headless_procs pop 注册表项 + 调 _kill_headless。

    回归:story 删除后 headless opencode 子进程仍占 ~591MB 的泄漏(服务器实测)。
    """

    class _FakeProc:
        def __init__(self, pid):
            self.pid = pid

        def poll(self):
            return None

    from story_lifecycle.orchestrator import executors as ex
    from story_lifecycle.orchestrator.engine import planner

    killed: list = []
    monkeypatch.setattr(planner, "_kill_headless", lambda proc: killed.append(proc))

    p_design = _FakeProc(111)
    p_impl = _FakeProc(222)
    ex._headless_procs[("s1", "design")] = p_design
    ex._headless_procs[("s1", "implement")] = p_impl
    ex._headless_procs[("s2", "design")] = _FakeProc(333)

    # story s1 全 stage → 杀 s1 的两个,保留 s2
    ex.kill_headless("s1")
    assert ("s1", "design") not in ex._headless_procs
    assert ("s1", "implement") not in ex._headless_procs
    assert ("s2", "design") in ex._headless_procs
    assert set(killed) == {p_design, p_impl}

    # 指定 stage → 只杀该 stage
    ex.kill_headless("s2", "design")
    assert ("s2", "design") not in ex._headless_procs

    # 幂等:再调不抛
    ex.kill_headless("s1")


def test_cleanup_headless_all_clears_registry(monkeypatch):
    """serve 关停时 cleanup_headless_all 杀掉所有残留 headless 进程。"""

    class _FakeProc:
        def __init__(self, pid):
            self.pid = pid

        def poll(self):
            return None

    from story_lifecycle.orchestrator import executors as ex
    from story_lifecycle.orchestrator.engine import planner

    killed: list = []
    monkeypatch.setattr(planner, "_kill_headless", lambda proc: killed.append(proc))

    ex._headless_procs[("s", "design")] = _FakeProc(1)
    ex._headless_procs[("s2", "build")] = _FakeProc(2)

    ex.cleanup_headless_all()
    assert ex._headless_procs == {}
    assert len(killed) == 2


def test_headless_watchdog_kills_on_timeout(monkeypatch):
    """看门狗:超时后 proc 仍活 → 强杀(防 supervise readline 无超时,上游 API 真挂)。"""
    import time as _time

    from story_lifecycle.orchestrator import executors as ex
    from story_lifecycle.orchestrator.engine import planner

    killed: list = []
    monkeypatch.setattr(planner, "_kill_headless", lambda p: killed.append(p))

    class _FakeProc:
        def poll(self):
            return None  # 一直活着

        @property
        def pid(self):
            return 999

    proc = _FakeProc()
    ex._arm_headless_watchdog("s1", "design", proc, timeout=0.2)
    _time.sleep(0.6)
    assert proc in killed


def test_headless_watchdog_noop_if_proc_done(monkeypatch):
    """看门狗:proc 已退出 → 不杀(幂等无害)。"""
    import time as _time

    from story_lifecycle.orchestrator import executors as ex
    from story_lifecycle.orchestrator.engine import planner

    killed: list = []
    monkeypatch.setattr(planner, "_kill_headless", lambda p: killed.append(p))

    class _FakeProc:
        def poll(self):
            return 0  # 已退出

        @property
        def pid(self):
            return 999

    proc = _FakeProc()
    ex._arm_headless_watchdog("s1", "design", proc, timeout=0.2)
    _time.sleep(0.6)
    assert killed == []
