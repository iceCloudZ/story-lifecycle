"""Tests for GET /api/story/{key}/sessions — status 从 PTY alive 实时派生 + 去重。

DESIGN-session-pty-id-model.md §3.4 / 问题 2、3:此前 DB 行的 status 直读静态值
('active'),且把 PTY 行当新行 append(去重失败)→ 同一会话出现两次 + 死进程
显示 active → 前端用 status==='running' 判断永远匹配不到。
"""
import story_lifecycle.orchestrator.service.api as api
import story_lifecycle.orchestrator.service.routers.sessions as sess_mod


def _stub_story(monkeypatch, story_key="S1"):
    """把 db.get_story 换成一个最小 story(避免 404)。"""
    monkeypatch.setattr(
        api.db, "get_story", lambda k: {"story_key": k, "workspace": "/tmp"}
    )


def _stub_db_sessions(monkeypatch, rows):
    monkeypatch.setattr(api.db, "list_sessions_for_story", lambda k: rows)


def _stub_pty_sessions(monkeypatch, ptys):
    """ptys: list of (stage, adapter, alive: bool)."""
    # 设计15 C3b: 路由移到 routers.sessions, mock 打在真实模块
    monkeypatch.setattr(
        sess_mod,
        "list_pty_sessions",
        lambda k: [
            {
                "session_id": "pty-key",  # key 内容不影响(按 stage,adapter 关联)
                "adapter": ad,
                "stage": st,
                "model": "",
                "status": "running" if alive else "exited",
                "started_at": "",
            }
            for (st, ad, alive) in ptys
        ],
    )


def test_status_running_when_pty_alive(monkeypatch):
    """DB 行存在 + PTY 活 → status=running(覆盖 DB 静态 active/completed)。"""
    _stub_story(monkeypatch)
    _stub_db_sessions(
        monkeypatch,
        [{"session_id": "uuid-1", "adapter": "claude", "stage": "design",
          "created_at": "2026-01-01 00:00:00"}],
    )
    _stub_pty_sessions(monkeypatch, [("design", "claude", True)])

    result = api.api_list_sessions("S1")
    sessions = result["sessions"]
    assert len(sessions) == 1
    assert sessions[0]["status"] == "running"
    assert sessions[0]["session_id"] == "uuid-1"  # DB 的 sid 保留


def test_status_exited_when_pty_dead_or_absent(monkeypatch):
    """DB 行存在 + PTY 死/无 → status=exited(不读 DB 的 active/completed)。"""
    _stub_story(monkeypatch)
    _stub_db_sessions(
        monkeypatch,
        [
            {"session_id": "uuid-1", "adapter": "claude", "stage": "design",
             "created_at": "t1"},
            {"session_id": "session_kimi", "adapter": "kimi", "stage": "build",
             "created_at": "t2"},
        ],
    )
    # PTY:design 死,build 无 → 都应 exited
    _stub_pty_sessions(monkeypatch, [("design", "claude", False)])

    result = api.api_list_sessions("S1")
    by_stage = {s["stage"]: s for s in result["sessions"]}
    assert by_stage["design"]["status"] == "exited"  # PTY 死
    assert by_stage["build"]["status"] == "exited"  # PTY 无


def test_no_duplicate_rows(monkeypatch):
    """同一 (stage,adapter) DB 行 + PTY 行不重复(问题 3)。

    修前:PTY 行因 session_id != DB uuid 被当新行 append → 同一会话两条。
    修后:PTY 只用来查存活态,不 append。
    """
    _stub_story(monkeypatch)
    _stub_db_sessions(
        monkeypatch,
        [{"session_id": "uuid-1", "adapter": "claude", "stage": "design",
          "created_at": "t1"}],
    )
    _stub_pty_sessions(monkeypatch, [("design", "claude", True)])

    result = api.api_list_sessions("S1")
    assert len(result["sessions"]) == 1  # 不重复


def test_kimi_association_by_stage_adapter_not_sid(monkeypatch):
    """kimi 的 DB session_id(捕获值)≠ PTY key(compute_session_id),
    但按 (stage,adapter) 关联仍能查到存活态。"""
    _stub_story(monkeypatch)
    _stub_db_sessions(
        monkeypatch,
        # kimi DB sid 是捕获值 session_<uuid>,跟 PTY key 字符串不同
        [{"session_id": "session_captured_abc", "adapter": "kimi", "stage": "verify",
          "created_at": "t1"}],
    )
    _stub_pty_sessions(monkeypatch, [("verify", "kimi", True)])

    result = api.api_list_sessions("S1")
    s = result["sessions"][0]
    assert s["status"] == "running"  # 按 (verify,kimi) 关联到了活 PTY
    assert s["session_id"] == "session_captured_abc"  # DB 的捕获值保留


def test_empty_when_no_db_rows(monkeypatch):
    """无 DB 行 → 空列表(不再 fallback append PTY-only 行)。"""
    _stub_story(monkeypatch)
    _stub_db_sessions(monkeypatch, [])
    _stub_pty_sessions(monkeypatch, [("design", "claude", True)])  # PTY 有

    result = api.api_list_sessions("S1")
    assert result["sessions"] == []  # 不 append PTY-only 行


def test_attach_id_from_live_pty(monkeypatch):
    """kimi 运行期 DB sid 未捕获(None) → attach_id 下发活 PTY 的注册表 id。

    回归(2026-07-27 tapd-1144381896001067713):session_id="" 时前端 chip
    setActiveSession("") 是 falsy → 永不渲染 TerminalPanel,「点不进去」。
    attach_id 是 /ws/pty/{story}/{id} 能用的凭据(compute_session_id)。
    """
    _stub_story(monkeypatch)
    _stub_db_sessions(
        monkeypatch,
        [{"session_id": None, "adapter": "kimi", "stage": "verify",
          "created_at": "t1"}],
    )
    _stub_pty_sessions(monkeypatch, [("verify", "kimi", True)])

    result = api.api_list_sessions("S1")
    s = result["sessions"][0]
    assert s["session_id"] == ""        # DB 占位语义不变
    assert s["status"] == "running"
    assert s["attach_id"] == "pty-key"  # WS attach 凭据


def test_attach_id_empty_when_no_live_pty(monkeypatch):
    """无活 PTY → attach_id 为空(前端据此走 resume 入口,不 attach 死会话)。"""
    _stub_story(monkeypatch)
    _stub_db_sessions(
        monkeypatch,
        [{"session_id": "session_abc", "adapter": "kimi", "stage": "verify",
          "created_at": "t1"}],
    )
    _stub_pty_sessions(monkeypatch, [("verify", "kimi", False)])

    result = api.api_list_sessions("S1")
    s = result["sessions"][0]
    assert s["status"] == "exited"
    assert s["attach_id"] == ""
