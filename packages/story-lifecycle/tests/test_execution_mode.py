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
    from story_lifecycle.orchestrator.engine.graph import find_ready_interactive_stories

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

    assert find_ready_interactive_stories() == ["READY-1"]


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

    assert calls[0][0][1] == ["claude-test"]
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
