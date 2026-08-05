from story_lifecycle.orchestrator.engine.profile_loader import resolve_profile
from story_lifecycle.orchestrator.engine.execution import auto_confirm_from_profile
from story_lifecycle.infra.db import models as db


def test_minimal_profile_defaults_to_interactive_pty():
    profile = resolve_profile("minimal")

    assert profile.execution_mode == "interactive_pty"
    assert profile.stage("design").execution_mode == "interactive_pty"


def test_swebench_profile_explicitly_uses_headless():
    profile = resolve_profile("swebench")

    assert profile.execution_mode == "headless"
    assert profile.stage("implement").execution_mode == "headless"


# ---- auto_confirm:supervisor 默认人工盯,False;仅全自动 profile 显式 True ----

def test_minimal_profile_defaults_to_manual_no_auto_confirm():
    """回归:普通 profile 默认 auto_confirm=False(人工盯,supervisor 不自动答)。

    旧默认是 supervisor 无条件 LLM 自动确认;翻转后默认不自动,需 profile 显式开启。
    """
    profile = resolve_profile("minimal")

    assert profile.auto_confirm is False
    assert profile.stage("design").auto_confirm is False
    assert auto_confirm_from_profile(profile) is False
    assert auto_confirm_from_profile(profile, "design") is False


def test_swebench_profile_auto_confirm_true():
    """swebench(benchmark 全自动)显式 auto_confirm=True。"""
    profile = resolve_profile("swebench")

    assert profile.auto_confirm is True
    assert auto_confirm_from_profile(profile) is True


def test_auto_confirm_from_profile_defensive_on_none():
    """profile=None / 缺字段 / 异常 → False(默认人工,绝不抛)。"""
    assert auto_confirm_from_profile(None) is False
    assert auto_confirm_from_profile(None, "implement") is False


def test_auto_confirm_stage_override_takes_precedence():
    """stage 级 auto_confirm 覆盖 profile 顶层(stage_cfg 已 merge,优先读它)。"""
    profile = resolve_profile("minimal")
    # 模拟某 stage 显式开了 auto_confirm(顶层仍 False)
    profile.stages["build"].auto_confirm = True

    # 顶层未开
    assert auto_confirm_from_profile(profile) is False
    # 该 stage 开了 → stage 级优先
    assert auto_confirm_from_profile(profile, "build") is True
    # 其他 stage 仍跟着顶层 False
    assert auto_confirm_from_profile(profile, "design") is False



def test_done_watcher_selects_only_ready_interactive_story(
    isolated_story_home, tmp_path
):
    """设计13:watcher(find_ready_interactive_stories)已删 —— 编排线程每轮扫
    active story,不需要「done 文件就绪」预筛(它会自己 poll artifacts)。"""
    from story_lifecycle.orchestrator.scheduler import (
        OrchestratorThread,
        list_active_for_orchestrator,
    )

    ready_workspace = tmp_path / "ready"
    ready_workspace.mkdir()
    done = ready_workspace / ".story" / "done" / "READY-1"
    done.mkdir(parents=True)
    (done / "design.json").write_text("{}", encoding="utf-8")

    db.upsert_story(
        "READY-1",
        workspace=str(ready_workspace),
        status="active",
    )
    db.update_story(
        "READY-1",
        context_json=(
            '{"_active_execution":{"stage":"design","mode":"interactive_pty"}}'
        ),
    )
    db.upsert_story(
        "PAUSED-1",
        workspace=str(ready_workspace),
        status="paused",
    )
    db.update_story(
        "PAUSED-1",
        context_json=(
            '{"_active_execution":{"stage":"design","mode":"interactive_pty"}}'
        ),
    )

    # 编排线程只驱动 status=active 的 story(paused 不碰)。
    active = list_active_for_orchestrator()
    assert "READY-1" in active
    assert "PAUSED-1" not in active


def test_terminal_spawn_starts_profile_agent_not_shell(
    isolated_story_home, tmp_path, monkeypatch
):
    import story_lifecycle.orchestrator.service.api as api
    from story_lifecycle.knowledge.adapters.base import SessionSpec

    db.upsert_story("TERM-1", workspace=str(tmp_path), profile="minimal")
    calls = []

    class FakeAdapter:
        # The spawner asks the adapter for a SessionSpec (command + how the
        # prompt is delivered). FakeAdapter fakes claude-style: command baked.
        def start_session(
            self, model, prompt="", session_id="", session_name="", resume=False
        ):
            return SessionSpec(
                command=["claude-test"],
                pty_prompt="",
                readiness_marker=None,
            )

    monkeypatch.setattr(api, "get_adapter", lambda name: FakeAdapter(), raising=False)
    monkeypatch.setattr(
        api,
        "ensure_agent_pty",
        # ensure_agent_pty returns (session_id, pty); append() returns None,
        # so fall through to a valid tuple to satisfy the caller's unpacking.
        lambda *args, **kwargs: calls.append((args, kwargs)) or ("session-1", object()),
        raising=False,
    )

    result = api.api_spawn_pty("TERM-1")

    # ensure_agent_pty(story_key, stage, adapter, command, ...) — command 在 args[3]
    assert calls[0][0][3] == ["claude-test"]
    assert result["purpose"] == "agent"


def test_planner_interactive_spawn_passes_read_file_seed_not_full_prompt(
    isolated_story_home, tmp_path, monkeypatch
):
    """Regression: continue_orchestrator_agent interactive 分支传给 start_session 的
    prompt 必须是「读 prompt_<stage>.md 文件」的短 seed,不能是完整多行 cli_prompt。

    历史 bug(tapd-1144381896001067642):planner.py:1067 把完整多行 cli_prompt 直接
    塞进 ``claude "query"``,claude CLI 只接收命令行首行 → agent 只拿到
    ``## 任务: verify`` 一行,无从下手。修复:与 _spawn_story_agent_pty(api.py)
    对齐,传读文件 seed,完整 prompt 落 prompt_<stage>.md。两条 spawn 入口路径一致。

    本测试锁定契约:不管哪条 spawn 路径,adapter.start_session 收到的都是 seed。
    """
    import json as _json

    from story_lifecycle.infra.terminal import pty as pty_mod
    from story_lifecycle.knowledge import adapters as adapters_mod
    from story_lifecycle.knowledge.adapters.base import SessionSpec
    from story_lifecycle.orchestrator.engine import planner

    db.upsert_story(
        "SEED-1",
        workspace=str(tmp_path),
        profile="minimal",  # interactive_pty
        current_stage="design",
        title="seed 投递回归",
    )
    # 走过规划:直接注入已确认的 action list(continue_orchestrator_agent 的输入)。
    db.update_story(
        "SEED-1",
        context_json=_json.dumps(
            {
                "_plan_confirmed": True,
                "_agent_actions": [
                    {
                        "action": "launch",
                        "adapter": "claude",
                        "stage": "design",
                        "focus": "设计冷却结清还款计划更新",
                        "task_actions": ["write_design_doc", "write_code"],
                        "done_file": ".story/done/SEED-1/design.json",
                    }
                ],
            },
            ensure_ascii=False,
        ),
    )

    captured = {}

    class FakeAdapter:
        name = "claude"

        def start_session(
            self, model, prompt="", session_id="", session_name="", resume=False
        ):
            captured["prompt"] = prompt
            return SessionSpec(
                command=["claude-fake"], pty_prompt="", readiness_marker=None
            )

    monkeypatch.setattr(adapters_mod, "get_adapter", lambda name: FakeAdapter())
    # 避免真起 PTY / supervisor 线程:ensure_agent_pty 返回占位 pty。
    # 占位 pty 需支持 clean_exit_pty 调的 .write() + done 轮询查的 .alive
    # (.alive=False → clean_exit_pty 立即返回 + 轮询认为 pty 已死,快速收尾)。
    from unittest.mock import MagicMock

    _fake_pty = MagicMock()
    _fake_pty.alive = False

    def _fake_ensure(*a, **kw):
        return ("sess-1", _fake_pty)

    monkeypatch.setattr(pty_mod, "ensure_agent_pty", _fake_ensure)

    # done file 在 spawn 之后由轮询发现;这里先不放,避免被「stage 已完成」
    # gate 跳过 spawn。轮询会等 —— 用 monkeypatch 让时间飞,并在 spawn 后立刻
    # 放 done file 让它快速收尾。
    import time as _time

    _done_written = {"v": False}

    def _fast_sleep(seconds):
        # spawn 走完后(act_idx>0 时 start_session 已被调),落 done 让轮询退出。
        if captured and not _done_written["v"]:
            d = tmp_path / ".story" / "done" / "SEED-1" / "design.json"
            d.parent.mkdir(parents=True, exist_ok=True)
            d.write_text(_json.dumps({"summary": "done"}), encoding="utf-8")
            _done_written["v"] = True

    monkeypatch.setattr(_time, "sleep", _fast_sleep)

    planner.continue_orchestrator_agent("SEED-1", headless=False)

    # 1) start_session 收到的是读文件 seed,不是完整多行 cli_prompt
    received = captured["prompt"]
    assert received.startswith("请读取"), f"expected seed, got: {received!r}"
    assert "prompt_design.md" in received
    # 完整 cli_prompt 的首行标记不该出现在 seed 里(若出现 = 退化回旧 bug)
    assert "## 任务:" not in received

    # 2) 完整 prompt 仍被写入 prompt_<stage>.md(两个 spawn 入口同一路径)
    prompt_file = tmp_path / ".story" / "context" / "SEED-1" / "prompt_design.md"
    assert prompt_file.exists()
    full = prompt_file.read_text(encoding="utf-8")
    assert full.startswith("## 任务: design")  # 完整 prompt 首行
    assert "设计冷却结清还款计划更新" in full  # focus 段在完整 prompt 里


def test_continue_verify_skips_gate_when_stuck_restart_no_artifacts(
    isolated_story_home, tmp_path, monkeypatch
):
    """Regression: verify 阶段被 stuck-diagnose 判 restart 时,done_data 未赋值,
    poll 循环 break 后落到 verify-gate 块 → 旧代码 ``done_data.get(...)`` 抛
    UnboundLocalError → 强制切 adapter(2026-07-27 真实事件:claude 跑 24min 产出
    spec.md,被误判 stuck → restart → 崩 → 切 kimi)。

    修复:poll 循环前初始化 ``done_data = None``,verify-gate 加守卫
    ``if stage == "verify" and done_data is not None``。restart 路径 done_data 仍
    None → 跳过 gate → idx+=1 取 retry action 重跑(本测试 mock 让 retry 也快速结束)。

    本测试锁定:restart break 后(1)不抛 UnboundLocalError,(2)插入 retry action。
    """
    import json as _json
    import time as _time
    from unittest.mock import MagicMock

    from story_lifecycle.infra.db import models as db
    from story_lifecycle.infra.terminal import pty as pty_mod
    from story_lifecycle.knowledge import adapters as adapters_mod
    from story_lifecycle.knowledge.adapters.base import SessionSpec
    from story_lifecycle.orchestrator.engine import planner
    from story_lifecycle.orchestrator.engine import supervisor as sup_mod

    db.upsert_story(
        "RESTART-1",
        workspace=str(tmp_path),
        profile="single-pass",  # 单 stage=verify,直接命中 verify-gate 块
        current_stage="verify",
        title="restart 回归",
    )
    db.update_story(
        "RESTART-1",
        context_json=_json.dumps(
            {
                "_plan_confirmed": True,
                "_agent_actions": [
                    {
                        "action": "launch",
                        "adapter": "claude",
                        "stage": "verify",
                        "focus": "verify 提额事件",
                        "task_actions": ["write_code", "run_tests"],
                        "done_file": ".story/done/RESTART-1/verify.json",
                    }
                ],
            },
            ensure_ascii=False,
        ),
    )

    class FakeAdapter:
        name = "claude"
        # claude 契约:启动即知确定性 sid(planner 1107 读这个标志)。
        prespecified_session_id = True
        readiness_marker = None

        def start_session(self, model, prompt="", session_id="", session_name="", resume=False):
            return SessionSpec(
                command=["claude-fake"], pty_prompt="", readiness_marker=None
            )

        def inject_prompt(self, prompt, story_key, stage):
            return None

        def write_anchor(self, story_key, stage, cwd, prompt_hash):
            return None

        def make_sid_capturer(self, *a, **kw):
            return None

        def capture_sid_post_exit(self, *a, **kw):
            return None

    monkeypatch.setattr(adapters_mod, "get_adapter", lambda name: FakeAdapter())

    # 占位 pty:alive=True 避开 1565 的"PTY 退出未落地"早退;让控制流走到 stuck 检测。
    _fake_pty = MagicMock()
    _fake_pty.alive = True
    _fake_pty.kill = MagicMock()

    def _fake_ensure(*a, **kw):
        return ("sess-restart", _fake_pty)

    monkeypatch.setattr(pty_mod, "ensure_agent_pty", _fake_ensure)

    # PtyLogger 占位:stuck 检测前置要求 _pty_logger is not None。
    _fake_logger = MagicMock()
    _fake_logger.log_dir = str(tmp_path / "pty_verify")
    _fake_logger.events_path = str(tmp_path / "pty_verify" / "events.jsonl")
    _fake_logger.log_event = MagicMock()
    import story_lifecycle.infra.terminal.pty_logger as plog_mod
    monkeypatch.setattr(plog_mod, "PtyLogger", lambda *a, **kw: _fake_logger)

    # read_events 返回一条 output 事件(让 _last_ts 能解析,避免 None 干扰)。
    def _fake_read_events(log_dir, limit=50):
        return [{"ts": "2026-07-27T00:00:00Z", "dir": "output", "type": "text", "text": "x"}]
    monkeypatch.setattr(plog_mod, "read_events", _fake_read_events)

    # detect_stuck:判定卡死(返回非空)→ 进 diagnose 分支。
    monkeypatch.setattr(
        sup_mod, "detect_stuck", lambda **kw: {"rule": "no_output_timeout", "reason": "静默"}
    )
    # escalate_stuck:restart 不走这条(走 diagnose),但同模块 import 时一起 mock 避免副作用。
    monkeypatch.setattr(sup_mod, "escalate_stuck", lambda **kw: None)

    # diagnose 路径:should_upgrade=False(走 summary)→ summary 返回 restart。
    import story_lifecycle.orchestrator.evaluation.stuck_diagnose as diag_mod
    monkeypatch.setattr(diag_mod, "should_upgrade_agentic", lambda *a, **kw: False)
    monkeypatch.setattr(
        diag_mod,
        "diagnose_stuck_summary",
        lambda **kw: {"action": "restart", "seed": "从检查点重试", "reason": "静默超时"},
    )

    # time.sleep 加速:poll 循环靠 elapsed += poll_interval(=5)累加,sleep 本身可空操作。
    # 不落地任何 artifact(模拟 restart 场景:卡死,产物未齐)。
    monkeypatch.setattr(_time, "sleep", lambda _s: None)

    # 关键断言:旧代码这里抛 UnboundLocalError;修复后应正常返回(retry action 也会被
    # 外层 while 取到再走一遍,但 ensure_agent_pty 占位 + sleep 加速会让它快速循环)。
    # 为避免 retry 无限循环,让第二次 spawn 后 pty.alive=False 触发 1565 早退 return。
    _spawn_count = {"n": 0}

    def _fake_ensure_counted(*a, **kw):
        _spawn_count["n"] += 1
        _p = MagicMock()
        # 第二次(retry)让 pty 已死 → 1565 早退 return,结束外层 while
        _p.alive = (_spawn_count["n"] == 1)
        return (f"sess-{_spawn_count['n']}", _p)

    monkeypatch.setattr(pty_mod, "ensure_agent_pty", _fake_ensure_counted)

    # 不应抛 UnboundLocalError
    planner.continue_orchestrator_agent("RESTART-1", headless=False)

    # restart 应插入了 retry action(原 1 个 → 现 ≥2 个)
    story = db.get_story("RESTART-1")
    import json as _j
    ctx = _j.loads(story["context_json"])
    actions = ctx.get("_agent_actions", [])
    assert len(actions) >= 2, f"restart 应插入 retry action,实际 {len(actions)} 个: {actions}"
    # retry action 的 focus 带 restart 标记
    retry = actions[1]
    assert "restart" in retry.get("focus", ""), f"retry action focus 异常: {retry}"


# ---- /advance 端点:active-unstarted 分支(single-pass 创建即 active 但从未启动) ----


def test_advance_starts_active_unstarted_story(isolated_story_home, tmp_path, monkeypatch):
    """PUT /advance 对 active 且无 _active_execution 的 story 触发 start_story_async。

    single-pass 等 profile 创建即 active,但执行从未触发。overview「开始执行」按钮
    调 /advance,这里断言它首次启动(而非像旧逻辑那样 active 时啥也不干返回 ok)。
    """
    import json as _json
    from unittest.mock import MagicMock

    from starlette.testclient import TestClient

    import story_lifecycle.orchestrator.service.api as api_mod
    from story_lifecycle.orchestrator.service.api import app

    db.upsert_story(
        "ADV-START-1",
        workspace=str(tmp_path),
        profile="single-pass",
        current_stage="verify",
        status="active",
    )
    # 无 _active_execution(从未启动)
    db.update_story(
        "ADV-START-1",
        context_json=_json.dumps({"prd_path": "x"}),
    )

    started = MagicMock()
    monkeypatch.setattr(api_mod, "start_story_async", started)

    client = TestClient(app)
    r = client.put("/api/story/ADV-START-1/advance")
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "started"
    started.assert_called_once_with("ADV-START-1")


def test_advance_does_not_restart_active_running_story(isolated_story_home, tmp_path, monkeypatch):
    """PUT /advance 对 active 且已有 _active_execution 的 story 不重复触发启动。

    已在跑的 story 不该被 /advance 再次 start(CAS 也会兜底,但提前返回避免抖动)。
    """
    import json as _json
    from unittest.mock import MagicMock

    from starlette.testclient import TestClient

    import story_lifecycle.orchestrator.service.api as api_mod
    from story_lifecycle.orchestrator.service.api import app

    db.upsert_story(
        "ADV-RUN-1",
        workspace=str(tmp_path),
        profile="single-pass",
        current_stage="verify",
        status="active",
    )
    # 有 _active_execution(已在跑)
    db.update_story(
        "ADV-RUN-1",
        context_json=_json.dumps(
            {"_active_execution": {"mode": "interactive_pty", "stage": "verify"}}
        ),
    )

    started = MagicMock()
    monkeypatch.setattr(api_mod, "start_story_async", started)

    client = TestClient(app)
    r = client.put("/api/story/ADV-RUN-1/advance")
    assert r.status_code == 200, r.text
    # 已在跑 → 不触发 start(返回默认 ok,无 status 字段)
    assert r.json() == {"ok": True}
    started.assert_not_called()


def test_spawn_session_unknown_adapter_returns_400(isolated_story_home, tmp_path):
    """POST /sessions/spawn 传未知 adapter → 400(不是 500)。

    get_adapter 对未知名抛 ValueError(消息含 builtin/configured 名单)。原先未捕获
    直接冒泡成 500;现在转成 400 + 透传消息,让前端能据此提示用户。
    """
    from starlette.testclient import TestClient

    from story_lifecycle.orchestrator.service.api import app

    db.upsert_story(
        "SPAWN-400-1",
        workspace=str(tmp_path),
        profile="minimal",
        current_stage="design",
        status="active",
    )
    client = TestClient(app)
    r = client.post(
        "/api/story/SPAWN-400-1/sessions/spawn",
        json={"adapter": "totally-bogus-cli"},
    )
    assert r.status_code == 400, f"expected 400, got {r.status_code}: {r.text}"
    # 消息透传 get_adapter 的报错(含 builtin 名单,帮用户纠错)
    assert "Unknown CLI adapter" in r.json()["detail"]
    assert "claude" in r.json()["detail"]  # builtin 列表里至少有 claude
