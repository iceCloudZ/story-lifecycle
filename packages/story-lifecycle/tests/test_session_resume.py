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
