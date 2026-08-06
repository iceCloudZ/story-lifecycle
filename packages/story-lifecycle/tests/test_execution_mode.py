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

    import story_lifecycle.orchestrator.service.routers.sessions as _sess_mod

    monkeypatch.setattr(
        _sess_mod, "get_adapter", lambda name: FakeAdapter(), raising=False
    )
    # 设计14(D3):spawn 主体收敛到 spawn_recipe,ensure_agent_pty 由 pty 模块
    # 提供(api 不再直接持有)—— 补丁打在真实调用点。
    import story_lifecycle.infra.terminal.pty as _pty_mod

    monkeypatch.setattr(
        _pty_mod,
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
    # 设计15 C3c: advance_story 移到 routers.lifecycle,patch 打在真实模块
    import story_lifecycle.orchestrator.service.routers.lifecycle as _lifecycle_mod

    monkeypatch.setattr(_lifecycle_mod, "start_story_async", started)

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
    # 设计15 C3c: advance_story 移到 routers.lifecycle,patch 打在真实模块
    import story_lifecycle.orchestrator.service.routers.lifecycle as _lifecycle_mod

    monkeypatch.setattr(_lifecycle_mod, "start_story_async", started)

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
