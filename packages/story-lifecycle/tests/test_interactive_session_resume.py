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
        self.tap_added = False
        self.tap_removed = False

    def add_tap(self, maxsize=512):
        self.tap_added = True
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


def _stub_spawn_deps(monkeypatch, db_row, capture=None):
    import story_lifecycle.infra.terminal.pty as pty_mod

    monkeypatch.setattr(api.db, "get_session", lambda *a: db_row)
    monkeypatch.setattr(api.db, "upsert_session", lambda *a, **k: None)
    monkeypatch.setattr(api, "_build_stage_launch_prompt", lambda s: "seed")

    def _fake_ensure(*a, **k):
        if capture is not None:
            capture.append(k.get("env"))
        return ("reg-id", _FakePty())

    # 设计14(D3):spawn 主体收敛到 spawn_recipe,ensure_agent_pty 由 pty 模块
    # 提供(api 不再直接持有)—— 补丁打在真实调用点。
    monkeypatch.setattr(pty_mod, "ensure_agent_pty", _fake_ensure)


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


def test_interactive_spawn_injects_story_env(monkeypatch, tmp_path):
    """交互式 PTY spawn 必须注入完整 STORY_* env(含 STORY_TITLE)。

    回归(2026-07-28):`_spawn_story_agent_pty` 此前不传 env → ensure_agent_pty 默认
    None → 子进程继承 serve 进程环境,什么 STORY_* 都没有 → code agent 跑
    `story tool declare` 时 ValueError 缺 story_key;且 STORY_TITLE 漏注入 →
    evidence 子目录 slug 退化成字面量 "需求",与 PRD 的 title-slug 目录不一致。
    """
    story = {
        "story_key": "S1",
        "workspace": str(tmp_path),
        "current_stage": "verify",
        "title": "【事件中心】新增提额成功事件",
        "context_json": "{}",
    }
    captured = []
    _stub_spawn_deps(monkeypatch, db_row={"session_id": None}, capture=captured)

    api._spawn_story_agent_pty(story, _FakeAdapter(), "sonnet")
    assert len(captured) == 1
    env = captured[0]
    assert env is not None, "ensure_agent_pty 未收到 env(交互式 spawn 漏注入)"
    assert env["STORY_KEY"] == "S1"
    assert env["STORY_STAGE"] == "verify"
    assert env["STORY_WORKSPACE"] == str(tmp_path)
    assert env["STORY_ADAPTER"] == "kimi"
    # 关键:STORY_TITLE 必须透传,否则 declare 算 evidence 子目录时 slug 退化成 "需求"。
    assert env["STORY_TITLE"] == "【事件中心】新增提额成功事件"


def _wait_session_id(story_key: str, stage: str, adapter: str, timeout: float = 5):
    """轮询 DB 直到 sid 回填(daemon 线程异步写)。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        row = db.get_session(story_key, stage, adapter)
        sid = row.get("session_id") if row else None
        if sid:
            return sid
        time.sleep(0.05)
    return None


def test_tap_capture_backfills_kimi_sid():
    """输出行捕获模型(kimi):PTY 输出含退出行 → 策略 arm 的 tap 线程回填 DB。

    走真实 ShellAdapter + arm_sid_capture + 真实 DB(隔离 STORY_HOME)。

    直接构造 ShellAdapter(name='kimi')而非 get_adapter('kimi'):kimi 是配置驱动
    的 adapter,CI 无 ~/.story-lifecycle/adapters.yaml 时 get_adapter 抛
    ValueError。本测试只验 sid 捕获行为(内置 kimi 退出行正则,不依赖 yaml),
    故直接实例化 —— 仍走真实 ShellAdapter,只是绕开会因缺配置失败的工厂。
    """
    from story_lifecycle.knowledge.adapters.shell import ShellAdapter
    from story_lifecycle.infra.terminal import sid_capture

    db.upsert_story(
        "S1", title="t", workspace="/tmp", profile="minimal",
        current_stage="verify", status="active",
    )
    db.upsert_session("S1", "verify", "kimi", session_id=None)

    pty = _FakePty()
    sid_capture.arm_sid_capture(
        ShellAdapter(name="kimi"), pty, story_key="S1", stage="verify"
    )
    assert pty.tap_added  # 输出行捕获 → 挂了 tap 线程
    pty.tap.put_nowait(
        "若干输出\nTo resume this session: kimi -r session_deadbeef\n".encode()
    )
    pty.alive = False  # 进程退出,线程应排空 tap 后摘除并退出

    assert _wait_session_id("S1", "verify", "kimi") == "session_deadbeef"

    # 线程应已退出并摘除 tap
    deadline = time.time() + 5
    while not pty.tap_removed and time.time() < deadline:
        time.sleep(0.05)
    assert pty.tap_removed


def test_post_exit_capture_backfills_opencode_sid():
    """文件扫描捕获模型(opencode):PTY 死亡后 capture_sid_post_exit 回填 DB。"""
    from story_lifecycle.infra.terminal import sid_capture

    db.upsert_story(
        "S2", title="t", workspace="/tmp", profile="minimal",
        current_stage="build", status="active",
    )
    db.upsert_session("S2", "build", "opencode", session_id=None)

    class _OpencodeLike:
        name = "opencode"
        prespecified_session_id = False

        def make_sid_capturer(self, *a, **k):
            return None  # opencode 终端不吐 sid

        def capture_sid_post_exit(self, story_key, stage, cwd, since_ts):
            return "ses_xyz789"  # 模拟查 opencode.db 命中

    pty = _FakePty()
    sid_capture.arm_sid_capture(
        _OpencodeLike(), pty, story_key="S2", stage="build"
    )
    assert not pty.tap_added  # 无输出行捕获 → 不挂 tap
    pty.alive = False  # 死亡后 watcher 调 capture_sid_post_exit

    assert _wait_session_id("S2", "build", "opencode") == "ses_xyz789"


def test_prespecified_adapter_arms_nothing():
    """prespecified sid 模型(claude):sid 在 NEW 时已入库,不 arm 任何捕获。"""
    from story_lifecycle.infra.terminal import sid_capture

    class _ClaudeLike:
        name = "claude"
        prespecified_session_id = True

        def make_sid_capturer(self, *a, **k):
            raise AssertionError("prespecified adapter 不应调捕获钩子")

        def capture_sid_post_exit(self, *a, **k):
            raise AssertionError("prespecified adapter 不应调捕获钩子")

    pty = _FakePty()
    sid_capture.arm_sid_capture(
        _ClaudeLike(), pty, story_key="S3", stage="design"
    )
    assert not pty.tap_added
