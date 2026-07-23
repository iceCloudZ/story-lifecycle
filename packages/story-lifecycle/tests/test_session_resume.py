"""Session resume (Approach A): claude --session-id + --resume for persistence.

Deterministic UUID per story+stage (uuid5) + a marker file decide NEW vs RESUME.
NEW:     claude --session-id <uuid> --name <key>-<stage> "<prompt>"
RESUME:  claude --resume <uuid> "<continue>"   (same cwd — resume lookup is cwd-scoped)
"""
import json
import uuid

from story_lifecycle.knowledge.adapters.claude import ClaudeAdapter


def test_launch_cmd_new_session_uses_session_id_and_name():
    a = ClaudeAdapter()
    sid = str(uuid.uuid5(uuid.NAMESPACE_DNS, "tapd-1:design"))
    cmd = a.interactive_launch_cmd(
        "sonnet", prompt="do design", session_id=sid, session_name="tapd-1-design", resume=False
    )
    assert "--session-id" in cmd and sid in cmd
    assert "--name" in cmd and "tapd-1-design" in cmd
    assert cmd[-1] == "do design"
    assert "--resume" not in cmd


def test_launch_cmd_resume_uses_resume_and_not_session_id_flag():
    a = ClaudeAdapter()
    sid = str(uuid.uuid5(uuid.NAMESPACE_DNS, "tapd-1:design"))
    cmd = a.interactive_launch_cmd("sonnet", prompt="继续", session_id=sid, resume=True)
    assert "--resume" in cmd and sid in cmd
    assert "--session-id" not in cmd  # resume doesn't re-declare --session-id
    assert cmd[-1] == "继续"


def test_launch_cmd_plain_no_session_still_works():
    # backward compat: planner path calls without session_id/session_name/resume
    a = ClaudeAdapter()
    assert a.interactive_launch_cmd("sonnet") == [a.interactive_launch_cmd("sonnet")[0]]
    cmd = a.interactive_launch_cmd("sonnet", prompt="hi")
    assert cmd[-1] == "hi"
    assert "--session-id" not in cmd and "--resume" not in cmd


def test_build_stage_launch_cmd_new_writes_marker(tmp_path, monkeypatch):
    import story_lifecycle.orchestrator.service.api as api

    story = {"story_key": "tapd-1", "workspace": str(tmp_path), "current_stage": "design", "profile": "minimal"}
    monkeypatch.setattr(api, "_build_stage_launch_prompt", lambda s: "READ-FILE-INSTR")
    cmd, is_resume = api._build_stage_launch_cmd(story, ClaudeAdapter(), "sonnet")
    assert is_resume is False
    assert "--session-id" in cmd
    assert cmd[-1] == "READ-FILE-INSTR"
    marker = tmp_path / ".story" / "context" / "tapd-1" / "session_design.json"
    assert marker.exists()
    data = json.loads(marker.read_text(encoding="utf-8"))
    assert data["name"] == "tapd-1-design"
    assert data["session_id"] == str(uuid.uuid5(uuid.NAMESPACE_DNS, "tapd-1:design"))


def test_build_stage_launch_cmd_resume_when_marker_exists(tmp_path, monkeypatch):
    import story_lifecycle.orchestrator.service.api as api

    story = {"story_key": "tapd-1", "workspace": str(tmp_path), "current_stage": "design", "profile": "minimal"}
    marker = tmp_path / ".story" / "context" / "tapd-1" / "session_design.json"
    marker.parent.mkdir(parents=True, exist_ok=True)
    sid = str(uuid.uuid5(uuid.NAMESPACE_DNS, "tapd-1:design"))
    marker.write_text(json.dumps({"session_id": sid, "name": "tapd-1-design"}), encoding="utf-8")
    # new-prompt builder must NOT be called on resume
    called = {"n": 0}
    def boom(s):
        called["n"] += 1
        return "SHOULD-NOT-BE-USED"
    monkeypatch.setattr(api, "_build_stage_launch_prompt", boom)
    cmd, is_resume = api._build_stage_launch_cmd(story, ClaudeAdapter(), "sonnet")
    assert is_resume is True
    assert called["n"] == 0
    assert "--resume" in cmd and sid in cmd
    assert "--session-id" not in cmd
    assert "SHOULD-NOT-BE-USED" not in cmd


# ---------------------------------------------------------------------------
# kimi (ShellAdapter) session resume + banner capture + DB persistence
# ---------------------------------------------------------------------------

def _kimi_adapter():
    from story_lifecycle.knowledge.adapters.shell import ShellAdapter

    return ShellAdapter(
        config={
            "binary": "kimi",
            "launch_cmd": "kimi",
            "inject_method": "stdin",
            "stdin_to_prompt_arg": True,
        },
        name="kimi",
    )


def test_kimi_new_session_no_resume_flag():
    """kimi 新会话:命令是裸 kimi(prompt 走 PTY paste),无 -S。"""
    kimi = _kimi_adapter()
    spec = kimi.start_session(model="", prompt="读 prompt.md", session_id="", resume=False)
    assert spec.command == ["kimi"]
    assert spec.pty_prompt == "读 prompt.md"
    assert spec.readiness_marker == "Welcome to Kimi Code"
    assert "-S" not in spec.command


def test_kimi_resume_uses_dash_S_with_session_id():
    """kimi resume:命令含 -S <id>,prompt 走 PTY paste。"""
    kimi = _kimi_adapter()
    spec = kimi.start_session(
        model="", prompt="继续", session_id="session_abc-123", resume=True
    )
    assert "-S" in spec.command
    assert "session_abc-123" in spec.command
    assert spec.pty_prompt == "继续"
    # 不该带 kimi 不认的 --session-id(claude 专属)
    assert "--session-id" not in spec.command


def test_kimi_resume_without_session_id_is_noop():
    """resume=True 但没 id(捕获失败兜底)→ 不加 -S,当新会话。"""
    kimi = _kimi_adapter()
    spec = kimi.start_session(model="", prompt="seed", session_id="", resume=True)
    assert "-S" not in spec.command


def test_kimi_session_capture_regex():
    """_capture_kimi_session 的正则能从 banner 输出解析 session_<uuid>。"""
    from story_lifecycle.orchestrator.engine.planner import _KIMI_SESSION_RE

    banner = "│  Session:   session_a273ffaa-8630-4315-96c1-4beca972b7db      │"
    m = _KIMI_SESSION_RE.search(banner)
    assert m is not None
    assert m.group(1) == "session_a273ffaa-8630-4315-96c1-4beca972b7db"
    # 不匹配的行(如 Model/Version)
    assert _KIMI_SESSION_RE.search("│  Model:     K3") is None


def test_story_session_db_crud(isolated_story_home):
    """story_session 表 CRUD:upsert / get / set_session_id / complete。"""
    from story_lifecycle.infra.db import models as db

    # 初始无记录
    assert db.get_session("S1", "design", "claude") is None
    # claude:spawn 前就给 uuid5
    db.upsert_session("S1", "design", "claude", session_id="uuid-claude-1")
    row = db.get_session("S1", "design", "claude")
    assert row is not None
    assert row["session_id"] == "uuid-claude-1"
    assert row["status"] == "active"
    # kimi:先占位(sid=None),捕获后回填
    db.upsert_session("S1", "design", "kimi", session_id=None)
    assert db.get_session("S1", "design", "kimi")["session_id"] is None
    db.set_session_id("S1", "design", "kimi", "session_captured")
    assert db.get_session("S1", "design", "kimi")["session_id"] == "session_captured"
    # upsert 不覆盖已有 sid(COALESCE)
    db.upsert_session("S1", "design", "claude", session_id=None)
    assert db.get_session("S1", "design", "claude")["session_id"] == "uuid-claude-1"
    # complete
    db.complete_session("S1", "design", "claude")
    assert db.get_session("S1", "design", "claude")["status"] == "completed"


class _FakePty:
    """Minimal PTY stub:立即吐一段 banner 输出给 tap,模拟 kimi 启动。

    _capture_kimi_session 只用 add_tap/remove_tap + tap.get_nowait(),这里实现这三者。
    """

    def __init__(self, banner_chunk: str):
        self._chunk = banner_chunk
        self._delivered = False

    def add_tap(self, maxsize: int = 512):
        return self

    def remove_tap(self, tap):
        pass

    def get_nowait(self):
        # 第一次取返回 banner,之后抛空(模拟「输出结束」)。
        import asyncio

        if not self._delivered:
            self._delivered = True
            return self._chunk
        raise asyncio.QueueEmpty


def test_capture_kimi_session_writes_db(isolated_story_home):
    """_capture_kimi_session 从 banner 输出解析 session_<uuid> 并回填 DB。

    这是半自动路径(api.py)+ 全自动路径(planner.py)共用的回填函数 ——
    kimi 的 session id 只能 spawn 后捕获,这函数是 resume 能否生效的关键。
    """
    from story_lifecycle.infra.db import models as db
    from story_lifecycle.orchestrator.engine.planner import _capture_kimi_session

    # 占位行(模拟 spawn 前的 upsert_session(sid=None))
    db.upsert_session("S2", "design", "kimi", session_id=None)
    assert db.get_session("S2", "design", "kimi")["session_id"] is None

    banner = (
        "│  Welcome to Kimi Code!                                              │\n"
        "│  Session:   session_a273ffaa-8630-4315-96c1-4beca972b7db            │\n"
        "│  Model:     K3                                                      │\n"
    )
    _capture_kimi_session("S2", "design", "kimi", _FakePty(banner))

    row = db.get_session("S2", "design", "kimi")
    assert row["session_id"] == "session_a273ffaa-8630-4315-96c1-4beca972b7db"


def test_capture_kimi_session_missing_banner_is_noop(isolated_story_home):
    """banner 没出现 session 行 → 不回填(下次当新会话),不崩。"""
    from story_lifecycle.infra.db import models as db
    from story_lifecycle.orchestrator.engine.planner import _capture_kimi_session

    db.upsert_session("S3", "build", "kimi", session_id=None)
    # banner 里没有 Session: 行
    _capture_kimi_session("S3", "build", "kimi", _FakePty("│  Welcome to Kimi Code!\n"))
    # sid 仍为 None(未捕获),但行还在(不崩)
    row = db.get_session("S3", "build", "kimi")
    assert row is not None
    assert row["session_id"] is None


def test_semiauto_kimi_path_calls_capture(tmp_path, monkeypatch):
    """半自动 _spawn_story_agent_pty 的 kimi 新会话会调 _capture_kimi_session。

    回归保护:之前半自动路径漏了捕获调用(只在全自动 planner 里加了),
    导致 kimi resume 在半自动下形同虚设。这里 spy 确认调用发生。
    """
    import story_lifecycle.orchestrator.service.api as api
    from story_lifecycle.knowledge.adapters.shell import ShellAdapter

    kimi_cfg = {
        "binary": "kimi",
        "launch_cmd": "kimi",
        "inject_method": "stdin",
        "stdin_to_prompt_arg": True,
    }
    kimi = ShellAdapter(config=kimi_cfg, name="kimi")
    story = {
        "story_key": "SEMI-1",
        "workspace": str(tmp_path),
        "current_stage": "design",
        "profile": "minimal",
    }
    monkeypatch.setattr(api, "_build_stage_launch_prompt", lambda s: "SEED")
    # ensure_agent_pty 返回 (session_id, fake_pty) — 不真 spawn
    monkeypatch.setattr(api, "ensure_agent_pty", lambda *a, **k: ("pty-1", object()))

    called = {"n": 0}

    def fake_capture(story_key, stage, adapter, pty):
        called["n"] += 1

    # api.py 里是延迟 import `from ..engine.planner import _capture_kimi_session`,
    # 故 patch 源头 planner._capture_kimi_session(api 每次 spawn 时现取)。
    import story_lifecycle.orchestrator.engine.planner as _planner

    monkeypatch.setattr(_planner, "_capture_kimi_session", fake_capture)

    api._spawn_story_agent_pty(story, kimi, model="")
    assert called["n"] == 1, "半自动 kimi 新会话应调 _capture_kimi_session 回填 sid"
