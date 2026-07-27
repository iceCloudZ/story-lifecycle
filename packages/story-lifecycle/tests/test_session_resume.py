"""Session resume (Approach A): claude --session-id + --resume for persistence.

Deterministic UUID per story+stage (uuid5) + a marker file decide NEW vs RESUME.
NEW:     claude --session-id <uuid> --name <key>-<stage> "<prompt>"
RESUME:  claude --resume <uuid> "<continue>"   (same cwd — resume lookup is cwd-scoped)
"""
import json
import uuid

from story_lifecycle.infra.db import models as db
from story_lifecycle.knowledge.adapters.claude import ClaudeAdapter


def test_launch_cmd_new_session_uses_session_id_and_name():
    a = ClaudeAdapter()
    sid = db.compute_session_id("tapd-1", "design", "claude")
    cmd = a.interactive_launch_cmd(
        "sonnet", prompt="do design", session_id=sid, session_name="tapd-1-design", resume=False
    )
    assert "--session-id" in cmd and sid in cmd
    assert "--name" in cmd and "tapd-1-design" in cmd
    assert cmd[-1] == "do design"
    assert "--resume" not in cmd


def test_launch_cmd_resume_uses_resume_and_not_session_id_flag():
    a = ClaudeAdapter()
    sid = db.compute_session_id("tapd-1", "design", "claude")
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
    # compute_session_id 三字段(story:stage:adapter),adapter=claude
    assert data["session_id"] == db.compute_session_id("tapd-1", "design", "claude")


def test_build_stage_launch_cmd_resume_when_marker_exists(tmp_path, monkeypatch):
    import story_lifecycle.orchestrator.service.api as api

    story = {"story_key": "tapd-1", "workspace": str(tmp_path), "current_stage": "design", "profile": "minimal"}
    marker = tmp_path / ".story" / "context" / "tapd-1" / "session_design.json"
    marker.parent.mkdir(parents=True, exist_ok=True)
    sid = db.compute_session_id("tapd-1", "design", "claude")
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


def test_kimi_session_capture_regex_exit_line():
    """ShellAdapter._exit_sid_pattern 能从 kimi 退出时的 resume 行解析 session_<uuid>。

    kimi 0.29.0 退出吐 'To resume this session: kimi -r session_<uuid>'(实测确认,
    DESIGN-session-pty-id-model.md §2.5.3)。旧正则匹配 'Session: <sid>' banner
    格式,与实际输出不符 → 从未工作(问题 9)。Phase 0 起正则从 planner 搬进 ShellAdapter。
    """
    kimi = _kimi_adapter()
    pattern = kimi._exit_sid_pattern()
    assert pattern is not None

    # 实测的真实退出输出格式
    exit_line = "To resume this session: kimi -r session_9807484b-4963-435b-ac07-1f59562f5bb1"
    m = pattern.search(exit_line)
    assert m is not None
    assert m.group(1) == "session_9807484b-4963-435b-ac07-1f59562f5bb1"
    # 也兼容旧 banner 格式(Session: <sid>),防其他 kimi 版本
    banner = "│  Session:   session_a273ffaa-8630-4315-96c1-4beca972b7db      │"
    assert pattern.search(banner).group(1) == "session_a273ffaa-8630-4315-96c1-4beca972b7db"
    # 不匹配无关行
    assert pattern.search("│  Model:     K3") is None


def test_prespecified_session_id_capability():
    """sid 模型是 adapter 的职责(Phase 0):claude 预指定、kimi 不预指定。

    spawner(api.py/planner.py)只读 adapter.prespecified_session_id 决定 NEW 时是否给 sid、
    是否在 stage-done 收尾时捕获,不再分支 adapter 名 —— 见 AGENTS.md「Session-id model」。
    """
    assert ClaudeAdapter().prespecified_session_id is True
    assert _kimi_adapter().prespecified_session_id is False
    # BaseAdapter 默认 False(基类未声明即「须捕获」)
    assert _kimi_adapter().capture_sid_post_exit("S", "design") is None
    # kimi 双保险:输出捕获(make_sid_capturer)+ 磁盘扫描(capture_sid_live /
    # capture_sid_post_exit,扫 ~/.kimi-code/sessions/)。cwd 为空时扫描安全返回
    # None(无前缀会把无关会话错误回填)。
    assert _kimi_adapter().make_sid_capturer("S", "design") is not None
    assert _kimi_adapter().capture_sid_live("S", "design", None, None) is None
    assert _kimi_adapter().capture_sid_post_exit("S", "design", None, None) is None
    # claude 不需要任何捕获钩子
    assert ClaudeAdapter().make_sid_capturer("S", "design") is None
    assert ClaudeAdapter().capture_sid_post_exit("S", "design") is None
    assert ClaudeAdapter().capture_sid_live("S", "design", None, None) is None


def test_compute_session_id_three_field():
    """compute_session_id 三字段,跨路径确定性(问题 4 核心)。

    DESIGN-session-pty-id-model.md §3.5:同一 (story,stage,adapter) 无论哪条
    spawn 路径算出的 sid 必须相同。此前 api.py 用 2 字段、planner.py 用 3 字段,
    算出不同 uuid → resume 续不上历史。
    """
    # 确定性:同输入同输出
    assert db.compute_session_id("S", "design", "claude") == db.compute_session_id("S", "design", "claude")
    # 三字段:含 adapter,不同 adapter 不同 sid(同 stage 可多 adapter)
    assert db.compute_session_id("S", "design", "claude") != db.compute_session_id("S", "design", "kimi")
    # 与旧 2 字段格式不同(回归保护:不能再退回 2 字段)
    old_2field = str(uuid.uuid5(uuid.NAMESPACE_DNS, "S:design"))
    assert db.compute_session_id("S", "design", "claude") != old_2field
    # 新增 opencode 也走三字段:与 claude/kimi 区分(同 stage 不同 adapter 不同 sid)
    assert db.compute_session_id("S", "design", "opencode") != db.compute_session_id("S", "design", "claude")
    assert db.compute_session_id("S", "design", "opencode") != db.compute_session_id("S", "design", "kimi")


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


def test_kimi_sid_capturer_writes_db(isolated_story_home):
    """ShellAdapter.make_sid_capturer 从退出输出解析 session_<uuid> 并回填 DB。

    DESIGN-session-pty-id-model.md §3.5 / 问题 9:捕获改在 clean_exit_pty 退出时,
    kimi 退出吐 'To resume this session: kimi -r session_<uuid>'。捕获器是
    on_output 回调,被 clean_exit_pty 在 drain 输出时调用。Phase 0 起从 planner 搬进
    ShellAdapter(由 config exit_sid_regex / kimi 默认正则驱动)。
    """
    # 占位行(模拟 spawn 前的 upsert_session(sid=None))
    db.upsert_session("S2", "design", "kimi", session_id=None)
    assert db.get_session("S2", "design", "kimi")["session_id"] is None

    # 模拟 clean_exit_pty drain 的退出输出(分块喂,模拟真实流式)
    capturer = _kimi_adapter().make_sid_capturer("S2", "design", "kimi")
    capturer("• 完成\n\n")
    capturer("To resume this session: kimi -r session_9807484b-4963-435b-ac07-1f59562f5bb1\n")

    row = db.get_session("S2", "design", "kimi")
    assert row["session_id"] == "session_9807484b-4963-435b-ac07-1f59562f5bb1"


def test_kimi_sid_capturer_no_resume_line_is_noop(isolated_story_home):
    """退出输出没出现 resume 行(kimi 崩溃/被 kill)→ 不回填,不崩。

    失败降级:下次当新会话(DESIGN-session-pty-id-model.md §3.5)。
    """
    db.upsert_session("S3", "build", "kimi", session_id=None)
    capturer = _kimi_adapter().make_sid_capturer("S3", "build", "kimi")
    # 只有普通输出,没有 resume 行
    capturer("• 做了一些事\n[Process killed]\n")

    row = db.get_session("S3", "build", "kimi")
    assert row is not None
    assert row["session_id"] is None


def test_kimi_sid_capturer_idempotent(isolated_story_home):
    """捕获器匹配到一次后短路,不再重复回填(避免多次 set_session_id)。"""
    db.upsert_session("S4", "verify", "kimi", session_id=None)
    capturer = _kimi_adapter().make_sid_capturer("S4", "verify", "kimi")
    # 喂两次 resume 行
    capturer("To resume this session: kimi -r session_aaaa-1111\n")
    capturer("To resume this session: kimi -r session_bbbb-2222\n")

    # 只回填第一次的
    row = db.get_session("S4", "verify", "kimi")
    assert row["session_id"] == "session_aaaa-1111"
