"""强制层测试 — advance_precheck_cmd 钩子（2026-08-26）。

事实链：配置了 advance_precheck_cmd 时，/lifecycle/advance 推进前执行外部校验——
非零退出码 / 超时 / 命令跑不起来 → 409（fail-closed）；未配置 → no-op。
"""

import pytest
from fastapi.testclient import TestClient

from story_lifecycle.orchestrator.service.api import app
from story_lifecycle.orchestrator.service.routers import lifecycle as lc


@pytest.fixture
def client():
    return TestClient(app)


def _cfg(monkeypatch, cmd=None):
    monkeypatch.setattr(
        "story_lifecycle.infra.config.get_config",
        lambda: ({"advance_precheck_cmd": cmd} if cmd else {}),
    )


def test_noop_when_not_configured(monkeypatch):
    _cfg(monkeypatch, None)
    lc._run_advance_precheck("tapd-x")  # 不抛


def test_blocks_on_nonzero_exit(monkeypatch):
    _cfg(monkeypatch, 'python -c "import sys; print(\'VIOLATION: bad branch\'); sys.exit(1)"')
    with pytest.raises(Exception) as ei:
        lc._run_advance_precheck("tapd-x")
    assert ei.value.status_code == 409
    assert "VIOLATION: bad branch" in ei.value.detail


def test_passes_on_zero_exit(monkeypatch):
    _cfg(monkeypatch, 'python -c "print(\'all green\')"')
    lc._run_advance_precheck("tapd-x")  # 不抛


def test_fail_closed_when_cmd_broken(monkeypatch):
    """检查器本身跑不起来也拒绝推进——坏检查器不许静默放行。"""
    _cfg(monkeypatch, "definitely-not-a-command-xyz-123")
    with pytest.raises(Exception) as ei:
        lc._run_advance_precheck("tapd-x")
    assert ei.value.status_code == 409


def test_endpoint_409_before_any_transition(client, monkeypatch):
    """端到端：precheck 失败时 409，先于一切状态变更。"""
    _cfg(monkeypatch, 'python -c "import sys; print(\'naming violation\'); sys.exit(1)"')
    monkeypatch.setattr(
        lc.db, "get_story",
        lambda sk: {"story_key": sk, "lifecycle_state": "开发", "context_json": "{}"},
    )
    r = client.post("/api/story/tapd-x/lifecycle/advance")
    assert r.status_code == 409
    assert "naming violation" in r.text


def test_put_lifecycle_forward_blocked(client, monkeypatch):
    """PUT /lifecycle 直跳前进态同样过钩子——直跳结项不能绕过强制层。"""
    _cfg(monkeypatch, 'python -c "import sys; print(\'no prod evidence\'); sys.exit(1)"')
    monkeypatch.setattr(
        lc.db, "get_story",
        lambda sk: {"story_key": sk, "lifecycle_state": "测试", "context_json": "{}"},
    )
    monkeypatch.setattr(lc.db, "update_story", lambda *a, **k: None)
    r = client.put("/api/story/tapd-x/lifecycle", json={"state": "结项"})
    assert r.status_code == 409
    assert "no prod evidence" in r.text


def test_put_lifecycle_backward_allowed(client, monkeypatch):
    """后退(纠错,如 结项→测试)不走钩子——历史违例不得锁死审计修正。"""
    _cfg(monkeypatch, 'python -c "import sys; sys.exit(1)"')  # 即使检查器必红也放行
    monkeypatch.setattr(
        lc.db, "get_story",
        lambda sk: {"story_key": sk, "lifecycle_state": "结项", "context_json": "{}"},
    )
    monkeypatch.setattr(lc.db, "update_story", lambda *a, **k: None)
    monkeypatch.setattr(lc.db, "log_event", lambda *a, **k: None)
    r = client.put("/api/story/tapd-x/lifecycle", json={"state": "测试"})
    assert r.status_code == 200


# ---- Q2 UI 确认门(2026-08-27) ----

def test_set_to_terminal_creates_pending_428(client, monkeypatch):
    """PUT 直跳终态:不再直接落位,返回 428 + 挂起 gate。"""
    monkeypatch.setattr("story_lifecycle.infra.config.get_config", lambda: {})
    state = {"lifecycle_state": "测试", "context_json": "{}"}
    monkeypatch.setattr(lc.db, "get_story", lambda sk: state)
    saved = {}
    monkeypatch.setattr(
        lc.db, "update_story",
        lambda sk, **kw: (state.update(kw), saved.update({"saved": True})),
    )
    r = client.put("/api/story/tapd-x/lifecycle", json={"state": "结项"})
    assert r.status_code == 428
    body = r.json()["detail"]  # HTTPException(428, {dict}) → 包裹层
    assert body["action"] == "ui_confirm"
    assert body["gate"]["target"] == "结项"
    # 关键:状态未变(挂起),仅 context_json 被写入
    assert state["lifecycle_state"] == "测试"


def test_ui_confirm_applies_set_transition(client, monkeypatch):
    """ui-upgrade confirm 重放 set 语义并清挂起。"""
    state = {
        "lifecycle_state": "测试",
        "context_json": '{"_upgrade_gate": {"target": "上线", "prev": "测试", "origin": "set"}}',
    }
    updates = []
    monkeypatch.setattr(lc.db, "get_story", lambda sk: state)
    monkeypatch.setattr(
        lc.db, "update_story",
        lambda sk, **kw: (updates.append(kw), state.update(kw)),
    )
    events = []
    monkeypatch.setattr(lc.db, "log_event", lambda *a, **k: events.append(k or a))
    r = client.post("/api/story/tapd-x/lifecycle/ui-upgrade", json={"action": "confirm"})
    assert r.status_code == 200 and r.json()["lifecycleState"] == "上线"
    assert any("_upgrade_gate" not in str(u) for u in [updates])
    assert '{"_upgrade_gate"' not in (state["context_json"])  # 挂起已清


def test_ui_reject_clears_without_state_change(client, monkeypatch):
    state = {
        "lifecycle_state": "测试",
        "context_json": '{"_upgrade_gate": {"target": "结项", "prev": "测试", "origin": "advance"}}',
    }
    monkeypatch.setattr(lc.db, "get_story", lambda sk: state)
    monkeypatch.setattr(lc.db, "update_story", lambda sk, **kw: state.update(kw))
    r = client.post("/api/story/tapd-x/lifecycle/ui-upgrade", json={"action": "reject"})
    assert r.status_code == 200 and r.json()["cleared"]
    assert "_upgrade_gate" not in state["context_json"]
    assert state["lifecycle_state"] == "测试"  # 状态未动


def test_ui_confirm_without_pending_409(client, monkeypatch):
    monkeypatch.setattr(
        lc.db, "get_story", lambda sk: {"context_json": "{}"}
    )
    r = client.post("/api/story/tapd-x/lifecycle/ui-upgrade", json={"action": "confirm"})
    assert r.status_code == 409
