"""交互式(UI)spawn 路径:会话复用 / resume 判据 / sid tap 捕获。

2026-07-27 tapd-1144381896001067713(verify · kimi)暴露:
- api_spawn_session 无复用检查 → 重复点击起重复进程、注册表条目被覆盖泄漏;
- marker 存在但 DB 无捕获 sid 时 resume=True → `kimi -S <uuid5>` 无效会话;
- 交互式 PTY 无人喂 make_sid_capturer → kimi sid 永不入库,resume 不可能。
"""

import asyncio
import time
from types import SimpleNamespace

import story_lifecycle.orchestrator.service.api as api
from story_lifecycle.infra.db import models as db


class _FakePty:
    """ManagedPty 的最小替身:alive/purpose/session_id + tap 队列。"""

    def __init__(self, session_id="reg-id", alive=True, purpose="agent"):
        self.session_id = session_id
        self.alive = alive
        self.purpose = purpose
        self.tap = asyncio.Queue()
        self.tap_removed = False

    def add_tap(self, maxsize=512):
        return self.tap

    def remove_tap(self, tap):
        self.tap_removed = True


def _stub_story(monkeypatch, tmp_path, story_key="S1", stage="verify"):
    story = {
        "story_key": story_key,
        "workspace": str(tmp_path),
        "current_stage": stage,
        "context_json": "{}",
    }
    monkeypatch.setattr(api.db, "get_story", lambda k: story)
    return story


def test_spawn_reuses_live_pty(monkeypatch, tmp_path):
    """该 (story,stage,adapter) 已有存活 agent PTY → 返回 reused,不再 spawn。

    此前每次点击都 ensure_agent_pty → spawn_pty 无条件覆盖注册表条目,
    旧进程泄漏(「启动终端」连点 = N 个 CLI 进程)。
    """
    _stub_story(monkeypatch, tmp_path)
    monkeypatch.setattr(api, "get_adapter", lambda name: object())
    monkeypatch.setattr(api, "get_pty", lambda k, sid="": _FakePty())

    def _boom(*a, **k):
        raise AssertionError("ensure_agent_pty must not be called when reusing")

    monkeypatch.setattr(api, "ensure_agent_pty", _boom)

    req = api.SpawnSessionRequest(adapter="kimi", model="")
    out = api.api_spawn_session("S1", req)
    assert out["reused"] is True
    assert out["session_id"] == "reg-id"
    assert out["ok"] is True


class _FakeAdapter:
    """ShellAdapter 替身:记录 start_session 入参;不接 sid 捕获。"""

    name = "kimi"
    prespecified_session_id = False

    def __init__(self):
        self.calls = []

    def start_session(self, model, prompt="", session_id="", session_name="",
                      resume=False):
        self.calls.append({"session_id": session_id, "resume": resume})
        return SimpleNamespace(
            command=["kimi"], pty_prompt="", readiness_marker=None
        )

    def make_sid_capturer(self, *a, **k):
        return None


def _stub_spawn_deps(monkeypatch, db_row):
    monkeypatch.setattr(api.db, "get_session", lambda *a: db_row)
    monkeypatch.setattr(api.db, "upsert_session", lambda *a, **k: None)
    monkeypatch.setattr(api, "_build_stage_launch_prompt", lambda s: "seed")
    monkeypatch.setattr(
        api, "ensure_agent_pty", lambda *a, **k: ("reg-id", _FakePty())
    )


def test_no_resume_without_captured_sid(monkeypatch, tmp_path):
    """marker 存在但 DB 无捕获 sid → kimi 不得 resume。

    回归:旧判据 `DB有sid or marker在` 会把 uuid5 当 sid 传给 `kimi -S`,
    指向一个不存在的会话。CLI 自分配 sid 的 adapter 只有捕获 sid 才能 resume。
    """
    story = _stub_story(monkeypatch, tmp_path)
    marker = tmp_path / ".story" / "context" / "S1" / "session_verify.json"
    marker.parent.mkdir(parents=True)
    marker.write_text("{}", encoding="utf-8")
    _stub_spawn_deps(monkeypatch, db_row={"session_id": None})

    adapter = _FakeAdapter()
    api._spawn_story_agent_pty(story, adapter, "sonnet")
    assert adapter.calls[0]["resume"] is False


def test_resume_with_captured_sid(monkeypatch, tmp_path):
    """DB 有捕获 sid → resume=True 且透传该 sid(`kimi -S session_xxx`)。"""
    story = _stub_story(monkeypatch, tmp_path)
    _stub_spawn_deps(monkeypatch, db_row={"session_id": "session_abc123"})

    adapter = _FakeAdapter()
    _, _, is_resume = api._spawn_story_agent_pty(story, adapter, "sonnet")
    assert is_resume is True
    assert adapter.calls[0]["resume"] is True
    assert adapter.calls[0]["session_id"] == "session_abc123"


def test_tap_capture_backfills_kimi_sid():
    """交互式 PTY 输出含 kimi 退出行 → tap 线程实时捕获并回填 DB。

    走真实 ShellAdapter.make_sid_capturer + 真实 DB(隔离 STORY_HOME)。
    """
    from story_lifecycle.knowledge.adapters import get_adapter

    db.upsert_story(
        "S1", title="t", workspace="/tmp", profile="minimal",
        current_stage="verify", status="active",
    )
    db.upsert_session("S1", "verify", "kimi", session_id=None)

    adapter = get_adapter("kimi")
    on_output = adapter.make_sid_capturer("S1", "verify", None, None)
    assert on_output is not None  # kimi 必须有退出行捕获

    pty = _FakePty()
    api._start_sid_capture_tap(pty, on_output)
    pty.tap.put_nowait(
        "若干输出\nTo resume this session: kimi -r session_deadbeef\n".encode()
    )
    pty.alive = False  # 进程退出,线程应排空 tap 后摘除并退出

    deadline = time.time() + 5
    captured = None
    while time.time() < deadline:
        row = db.get_session("S1", "verify", "kimi")
        captured = row.get("session_id") if row else None
        if captured:
            break
        time.sleep(0.05)
    assert captured == "session_deadbeef"

    # 线程应已退出并摘除 tap
    deadline = time.time() + 5
    while not pty.tap_removed and time.time() < deadline:
        time.sleep(0.05)
    assert pty.tap_removed
