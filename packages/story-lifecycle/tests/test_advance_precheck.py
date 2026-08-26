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
