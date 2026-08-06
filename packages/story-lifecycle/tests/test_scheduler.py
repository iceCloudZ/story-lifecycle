"""Tests for scheduler Decider(层5 多 story 调度)。

``decide_schedule`` 按 **优先级 + 就绪态 + FIFO** 排序多个 story,替 ``graph.py``
``max_workers=4`` 的纯 FIFO。纯函数,零副作用。

排序键:(就绪, 优先级, 创建时间)—— 就绪的先跑(blocked 的现在跑不了),
同就绪按优先级(P0 最高),同优先级 FIFO(created_at 早的先)。
"""

import pytest

from story_lifecycle.orchestrator.engine.scheduler import decide_schedule


def st(key, priority="P2", ready=True, created_at="2026-01-01 00:00:00"):
    return {
        "story_key": key,
        "priority": priority,
        "ready": ready,
        "created_at": created_at,
    }


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
        order = decide_schedule(
            stories=[
                st("LO", "low", created_at="t1"),
                st("HI", "high", created_at="t2"),
                st("MD", "medium", created_at="t3"),
            ]
        )
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
    """graph.order_ready_stories 按 decide_schedule 优先级排(替 FIFO)。

    设计13 后 graph.order_ready_stories 已删(watcher 机制),但 decide_schedule
    纯函数保留 —— 编排线程用它给多 story 排序。这里直接测 decide_schedule。
    """
    from story_lifecycle.orchestrator.engine.scheduler import decide_schedule

    ordered = decide_schedule(
        stories=[
            {"story_key": "LO", "priority": "P2", "ready": True, "created_at": "t1"},
            {"story_key": "HI", "priority": "P0", "ready": True, "created_at": "t2"},
            {"story_key": "MID", "priority": "P1", "ready": True, "created_at": "t3"},
        ]
    )
    assert ordered[0] == "HI"  # P0 最高
    assert ordered[1] == "MID"  # P1
    assert ordered[2] == "LO"  # P2


def test_order_ready_stories_empty():
    from story_lifecycle.orchestrator.engine.scheduler import decide_schedule

    assert decide_schedule(stories=[]) == []


def test_order_ready_stories_missing_story_row_kept():
    """decide_schedule 只排传入的 story,查不到的行由调用方丢弃(不崩)。"""
    from story_lifecycle.orchestrator.engine.scheduler import decide_schedule

    ordered = decide_schedule(
        stories=[
            {"story_key": "KEEP", "priority": "P0", "ready": True, "created_at": "t1"}
        ]
    )
    assert ordered == ["KEEP"]


# ---------------------------------------------------------------------------
# 设计 13 Step 5：OrchestratorThread 调度测试（mock executor + handler，不真起 PTY/LLM）
# ---------------------------------------------------------------------------

import json as _json

import pytest as _pytest

from story_lifecycle.orchestrator.scheduler import OrchestratorThread
from story_lifecycle.orchestrator.executors import InteractiveStageExecutor


class _FakePty:
    def __init__(self, alive=True):
        self.alive = alive
        self.session_id = "sess-fake"

    def kill(self):
        self.alive = False


@pytest.fixture
def _orchestrator():
    """一个不自动跑的编排线程实例（测试手动调 _tick）。"""
    thr = OrchestratorThread(poll_interval=0.01)
    yield thr
    thr.stop()
    thr._executor_pool.shutdown(wait=False)


def _make_active_story(tmp_path, story_key="S-SCHED-1", profile="minimal"):
    """创建 status=active、_plan_confirmed、有 _agent_actions 的 story。"""
    ws = tmp_path / "ws"
    ws.mkdir()
    db.create_story(story_key, "调度测试", str(ws), profile=profile)
    ctx = {
        "_plan_confirmed": True,
        "_agent_actions": [
            {"action": "launch", "stage": "design", "adapter": "claude"},
        ],
    }
    db.update_story(
        story_key,
        lifecycle_state="待启动",
        context_json=_json.dumps(ctx, ensure_ascii=False),
    )
    return story_key


class TestOrchestratorTick:
    def test_tick_skips_non_ready_story(
        self, tmp_path, isolated_story_home, _orchestrator
    ):
        """candidate 状态的 story 不被编排"""
        ws = tmp_path / "ws"
        ws.mkdir()
        db.create_story("S-CAND", "candidate", str(ws))
        db.update_story("S-CAND", intake_state="candidate")
        _orchestrator._tick()
        story = db.get_story("S-CAND")
        assert story["status"] == "active"  # 未被暂停/推进,也没有事件
        events = db.get_story_events("S-CAND")
        assert events == []

    def test_tick_polls_alive_pty_artifacts(
        self, tmp_path, isolated_story_home, _orchestrator
    ):
        """PTY 活着 + 没 artifacts → 不 judge"""
        _make_active_story(tmp_path)
        # mock get_pty 返回活 PTY,is_artifacts_ready False
        _orchestrator._tick()
        assert len(_orchestrator._judging) == 0

    def test_tick_judges_when_artifacts_ready(
        self, tmp_path, isolated_story_home, _orchestrator, monkeypatch
    ):
        """PTY 死了 + artifacts ready → submit judge"""
        key = _make_active_story(tmp_path)
        # 落地 design 成果物（story/spec.md）
        import story_lifecycle.infra.story_paths as sp
        from pathlib import Path as _P

        monkeypatch.setattr(sp, "story_evidence_root", lambda ws: _P(str(ws)) / "story")
        story = db.get_story(key)
        edir = sp.story_evidence_dir(story["workspace"], key, story["title"])
        edir.mkdir(parents=True, exist_ok=True)
        (edir / "spec.md").write_text("# spec", encoding="utf-8")
        # mock 一个死了的 PTY（设计循环语义：PTY 死了 + artifacts ready → judge）
        dead = _FakePty(alive=False)
        monkeypatch.setattr(
            "story_lifecycle.orchestrator.executors.InteractiveStageExecutor.get_pty",
            lambda self, k, st: dead,
        )
        _orchestrator._tick()
        # judge 被提交（_judging 里有 key 或结果已写）
        assert (
            f"{key}:design" in _orchestrator._judging
            or f"{key}:design" in _orchestrator._judge_results
        )

    def test_tick_skips_already_judging(
        self, tmp_path, isolated_story_home, _orchestrator
    ):
        """已在 judge → 不重复 submit"""
        key = _make_active_story(tmp_path)
        _orchestrator._judging.add(f"{key}:design")
        _orchestrator._tick()
        assert f"{key}:design" in _orchestrator._judging  # 仍在（没重复提交后被清）

    def test_tick_does_not_spawn_in_interactive(
        self, tmp_path, isolated_story_home, _orchestrator
    ):
        """半自动 + 没 PTY → 不 spawn"""
        key = _make_active_story(tmp_path)
        _orchestrator._tick()
        assert InteractiveStageExecutor().get_pty(key, "design") is None

    def test_tick_spawns_in_automatic(
        self, tmp_path, isolated_story_home, _orchestrator, monkeypatch
    ):
        """全自动 + 没 PTY → spawn"""
        _make_active_story(tmp_path, profile="single-pass")
        spawned = {}
        monkeypatch.setattr(
            "story_lifecycle.orchestrator.executors.AutomaticStageExecutor.spawn",
            lambda self, k, st, act: (
                spawned.setdefault("n", 0) or spawned.__setitem__("n", spawned["n"] + 1)
            ),
        )
        _orchestrator._tick()
        assert spawned.get("n", 0) >= 1


class TestOrchestratorCrashRecovery:
    def test_tick_survives_exception(
        self, tmp_path, isolated_story_home, _orchestrator, monkeypatch
    ):
        """executor 抛异常 → 不崩，继续"""
        _make_active_story(tmp_path)
        calls = {"n": 0}

        def _boom(*a, **kw):
            calls["n"] += 1
            raise RuntimeError("boom")

        monkeypatch.setattr(
            "story_lifecycle.orchestrator.executors.InteractiveStageExecutor.get_pty",
            _boom,
        )
        _orchestrator._tick()  # 不抛
        _orchestrator._tick()  # 下一轮正常
        assert calls["n"] >= 2


class TestJudgeTask:
    def test_judge_writes_result(
        self, tmp_path, isolated_story_home, _orchestrator, monkeypatch
    ):
        """judge_task 写结果到 _judge_results"""
        key = _make_active_story(tmp_path)
        monkeypatch.setattr(
            "story_lifecycle.orchestrator.evaluation.stage_completion.judge_stage_completion",
            lambda req: {
                "quality": "approve",
                "lifecycle_target": None,
                "summary": "ok",
                "reason": "",
            },
        )
        story = db.get_story(key)
        _orchestrator._judge_task(
            key,
            "design",
            {"stage": "design"},
            _json.loads(story["context_json"]),
            story,
        )
        result = _orchestrator._take_judge_result(key, "design")
        assert result is not None
        assert result["quality"] == "approve"

    def test_judge_fallback_on_llm_failure(
        self, tmp_path, isolated_story_home, _orchestrator, monkeypatch
    ):
        """LLM 挂了 → fallback approve"""
        key = _make_active_story(tmp_path)
        monkeypatch.setattr(
            "story_lifecycle.orchestrator.evaluation.stage_completion.judge_stage_completion",
            lambda req: (_ for _ in ()).throw(RuntimeError("llm down")),
        )
        story = db.get_story(key)
        _orchestrator._judge_task(
            key,
            "design",
            {"stage": "design"},
            _json.loads(story["context_json"]),
            story,
        )
        result = _orchestrator._take_judge_result(key, "design")
        assert result is not None
        assert result["quality"] == "approve"


class TestJudgeArtifactNormalization:
    """回归 1068018 事故：judge 必须与 is_artifacts_ready 用同一套成果物发现。"""

    def test_judge_receives_evidence_candidates(
        self, tmp_path, isolated_story_home, _orchestrator, monkeypatch
    ):
        """judge_stage_completion 收到 artifacts + evidence_candidates（含 evidence 兜底路径）。

        缺陷 1 回归：agent 把 spec.md 写到 evidence 目录，is_artifacts_ready 命中
        （带 evidence 兜底）→ submit judge，但 judge 没收到 evidence_candidates →
        读空 → 误 reject。修后 _judge_task 必须透传 resolve_stage_artifacts 的结果。
        """
        key = _make_active_story(tmp_path)
        import story_lifecycle.infra.story_paths as sp
        from pathlib import Path as _P

        monkeypatch.setattr(sp, "story_evidence_root", lambda ws: _P(str(ws)) / "story")
        story = db.get_story(key)
        edir = sp.story_evidence_dir(story["workspace"], key, story["title"])
        edir.mkdir(parents=True, exist_ok=True)
        (edir / "spec.md").write_text("# 方案\n提现门槛", encoding="utf-8")
        # 捕获 judge_stage_completion 收到的 JudgeRequest 字段
        captured = {}

        def _capture(req):
            captured.update(req.__dict__)
            return {
                "quality": "approve",
                "lifecycle_target": None,
                "summary": "ok",
                "reason": "",
            }

        monkeypatch.setattr(
            "story_lifecycle.orchestrator.evaluation.stage_completion.judge_stage_completion",
            _capture,
        )
        story = db.get_story(key)
        _orchestrator._judge_task(
            key,
            "design",
            {"stage": "design"},
            _json.loads(story["context_json"]),
            story,
        )
        # 透传了 artifacts（profile 声明的 story/spec.md）
        assert captured.get("artifacts") == ["story/spec.md"]
        # 透传了 evidence_candidates，且含 evidence 目录的 spec.md 兜底路径
        ev = captured.get("evidence_candidates") or {}
        assert "story/spec.md" in ev
        assert any("spec.md" in p for p in ev["story/spec.md"])


class TestStageFinalizeTiming:
    """回归 1068018 事故：收尾（记 completed / commit / 杀 PTY）只在 approve 后做。"""

    def test_no_finalize_before_judge_result(
        self, tmp_path, isolated_story_home, _orchestrator, monkeypatch
    ):
        """_submit_judge 提交 judge 后、judge 出结果前，不杀 PTY、不记 completed。

        缺陷 2 回归：旧 _submit_judge 在 submit judge 前就 _release_stage（杀 PTY）
        + log_event("completed")，judge reject 时 PTY 已死没法 resume 救场。
        """
        key = _make_active_story(tmp_path)
        import story_lifecycle.infra.story_paths as sp
        from pathlib import Path as _P

        monkeypatch.setattr(sp, "story_evidence_root", lambda ws: _P(str(ws)) / "story")
        story = db.get_story(key)
        edir = sp.story_evidence_dir(story["workspace"], key, story["title"])
        edir.mkdir(parents=True, exist_ok=True)
        (edir / "spec.md").write_text("# spec", encoding="utf-8")
        # 活的 PTY（关键：alive=True，模拟 claude 还在跑、成果物刚落地）
        live = _FakePty(alive=True)
        monkeypatch.setattr(
            "story_lifecycle.orchestrator.executors.InteractiveStageExecutor.get_pty",
            lambda self, k, st: live,
        )
        # kill 计数器
        kills = {"n": 0}
        _orig_kill = live.kill

        def _counting_kill():
            kills["n"] += 1
            _orig_kill()

        live.kill = _counting_kill
        # judge 先不返回（模拟还在跑）——用一个延迟返回的 stub
        _orchestrator._force_auto = False
        executor = InteractiveStageExecutor()
        _orchestrator._executors[key] = executor
        # 触发 _submit_judge（_tick_story 走 PTY 活+artifacts ready 分支）
        _orchestrator._tick()
        # _submit_judge 之后、_judge_task 还在线程池跑（或刚写结果）。
        # 关键断言：PTY 没被杀（_release_stage 不该在 judge 出结果前调）
        assert kills["n"] == 0, "PTY 在 judge 出结果前被杀了"
        assert live.alive is True, "活的 PTY 不该被提前 kill"
        # completed 事件不该在 judge 出结果前记（只有 approve 后才记）
        events = db.get_story_events(key)
        assert not any(e.get("event_type") == "completed" for e in events), (
            "completed 事件在 judge 出结果前就记了"
        )
        # 清理：让 judge 跑完（避免线程池残留影响后续测试）
        _orchestrator._executor_pool.shutdown(wait=True)

    def test_finalize_on_approve_releases_pty(
        self, tmp_path, isolated_story_home, _orchestrator, monkeypatch
    ):
        """approve 后 _finalize_stage_pass 才杀 PTY + 记 completed。"""
        key = _make_active_story(tmp_path)
        import story_lifecycle.infra.story_paths as sp
        from pathlib import Path as _P

        monkeypatch.setattr(sp, "story_evidence_root", lambda ws: _P(str(ws)) / "story")
        story = db.get_story(key)
        edir = sp.story_evidence_dir(story["workspace"], key, story["title"])
        edir.mkdir(parents=True, exist_ok=True)
        (edir / "spec.md").write_text("# spec", encoding="utf-8")
        # 新口径：PTY 活时只认 declare event（1068018 归一化）→ 必须 declare 才触发 submit judge
        db.log_event(
            key,
            "design",
            "artifact_declared",
            {"doc_type": "spec", "version": 1, "summary": "ok", "files_changed": []},
        )
        # judge 返回 approve
        monkeypatch.setattr(
            "story_lifecycle.orchestrator.evaluation.stage_completion.judge_stage_completion",
            lambda req: {
                "quality": "approve",
                "lifecycle_target": None,
                "summary": "ok",
                "reason": "",
            },
        )
        live = _FakePty(alive=True)
        monkeypatch.setattr(
            "story_lifecycle.orchestrator.executors.InteractiveStageExecutor.get_pty",
            lambda self, k, st: live,
        )
        executor = InteractiveStageExecutor()
        _orchestrator._executors[key] = executor
        # 走一轮 _tick → submit judge → _judge_task 写结果
        _orchestrator._tick()
        _orchestrator._executor_pool.shutdown(wait=True)
        # 下一轮 _tick → _handle_decision(approve) → _finalize_stage_pass
        _orchestrator._tick()
        # approve 后 PTY 被释放、completed 被记
        events = db.get_story_events(key)
        assert any(e.get("event_type") == "completed" for e in events), (
            "approve 后该记 completed 事件"
        )

    def test_build_commit_only_on_approve(
        self, tmp_path, isolated_story_home, _orchestrator, monkeypatch
    ):
        """build 阶段 reject 不 commit；approve 才 commit。

        缺陷 2 回归：旧 _submit_judge 在 judge 出结果前就 _auto_commit_worktrees，
        reject 时把废代码固化进分支历史。修后 commit 只在 approve 后做。
        直接测 _handle_decision 对 commit 的调度，绕过 is_artifacts_ready 的真实
        git 检测（commit 时机是本测试的关注点，不是 git 落地）。
        """
        key = _make_active_story(tmp_path)
        ctx = {
            "_plan_confirmed": True,
            "_completed_stages": ["design"],
            "_agent_actions": [
                {"action": "launch", "stage": "design", "adapter": "claude"},
                {"action": "launch", "stage": "build", "adapter": "opencode"},
            ],
        }
        db.update_story(
            key,
            current_stage="build",
            context_json=_json.dumps(ctx, ensure_ascii=False),
        )
        story = db.get_story(key)
        commits = {"n": 0}

        def _fake_commit(*a, **kw):
            commits["n"] += 1

        monkeypatch.setattr(
            "story_lifecycle.orchestrator.engine.planner._auto_commit_worktrees",
            _fake_commit,
        )
        executor = InteractiveStageExecutor()
        _orchestrator._executors[key] = executor
        with _orchestrator._lock:
            _orchestrator._stage_done_data[f"{key}:build"] = {
                "stage": "build",
                "summary": "build done",
            }
        from story_lifecycle.orchestrator.handlers import make_decision_handler

        handler = make_decision_handler(story, ctx)
        # reject → 不 commit
        _orchestrator._handle_decision(
            key,
            "build",
            {"quality": "reject", "reason": "不合格"},
            ctx,
            ctx["_agent_actions"],
            handler,
            executor,
            story,
        )
        assert commits["n"] == 0, "reject 时不该 commit"
        # approve → commit 一次
        _orchestrator._handle_decision(
            key,
            "build",
            {"quality": "approve", "summary": "ok", "reason": ""},
            ctx,
            ctx["_agent_actions"],
            handler,
            executor,
            story,
        )
        assert commits["n"] == 1, "approve 后该 commit 一次"
