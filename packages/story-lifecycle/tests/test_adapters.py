"""Tests for ShellAdapter and adapter registry."""

import json
import os

import pytest
from unittest.mock import patch

from story_lifecycle.knowledge.adapters import get_adapter
from story_lifecycle.knowledge.adapters.base import BaseAdapter
from story_lifecycle.knowledge.adapters.claude import ClaudeAdapter
from story_lifecycle.knowledge.adapters.codex import CodexAdapter
from story_lifecycle.knowledge.adapters.shell import ShellAdapter


class TestShellAdapter:
    def test_launch_cmd_with_model(self):
        adapter = ShellAdapter(config={"launch_cmd": "aider --model {model}"})
        assert adapter.launch_cmd("gpt-4") == "aider --model gpt-4"

    def test_launch_cmd_empty_config(self):
        adapter = ShellAdapter(config={})
        assert adapter.launch_cmd("sonnet") == ""

    def test_inject_prompt_stdin(self, tmp_path):
        adapter = ShellAdapter(config={"inject_method": "stdin"})
        result = adapter.inject_prompt("hello world", "test-key", "design")
        assert result is not None
        assert "cat" in result

    def test_inject_prompt_paste(self):
        adapter = ShellAdapter(config={"inject_method": "paste"})
        result = adapter.inject_prompt("hello", "key", "stage")
        assert result is None

    def test_inject_prompt_default(self):
        adapter = ShellAdapter(config={})
        result = adapter.inject_prompt("hello", "key", "stage")
        assert result is None

    def test_switch_provider_returns_none(self):
        adapter = ShellAdapter()
        assert adapter.switch_provider("openai") is None

    def test_cleanup_does_not_crash(self):
        adapter = ShellAdapter()
        adapter.cleanup("key", "stage")  # should not raise


class TestOpencodeAdapter:
    """OpenCode adapter: claude 式 baked-in prompt(--prompt 自动提交)+ 文件扫描
    捕获 sid(CLI 自分配 ses_…)。见 AGENTS.md「Session-id model」。"""

    def test_start_session_bakes_prompt_into_command(self):
        # opencode --prompt "..." 在 TUI 就绪后自动提交 → spawner 不注入 PTY。
        from story_lifecycle.knowledge.adapters.opencode import OpencodeAdapter

        a = OpencodeAdapter()
        spec = a.start_session(
            model="anthropic/claude-sonnet", prompt="读 prompt.md", resume=False
        )
        assert "--prompt" in spec.command
        assert "读 prompt.md" in spec.command
        assert "--model" in spec.command
        assert "anthropic/claude-sonnet" in spec.command
        assert "--auto" in spec.command
        # claude 式:不在 PTY 注入 prompt,不猜 readiness(banner 是 ASCII art 无锚点)
        assert spec.pty_prompt == ""
        assert spec.readiness_marker is None

    def test_resume_uses_session_flag(self):
        from story_lifecycle.knowledge.adapters.opencode import OpencodeAdapter

        a = OpencodeAdapter()
        spec = a.start_session(
            model="", prompt="继续", session_id="ses_abc123", resume=True
        )
        assert "--session" in spec.command
        assert "ses_abc123" in spec.command
        assert "--prompt" in spec.command  # resume 也带 continue seed

    def test_new_session_has_no_session_flag(self):
        from story_lifecycle.knowledge.adapters.opencode import OpencodeAdapter

        a = OpencodeAdapter()
        spec = a.start_session(model="", prompt="seed", resume=False)
        assert "--session" not in spec.command

    def test_sid_model_not_prespecified(self):
        # CLI 自分配 sid,须文件扫描捕获 —— 与 claude(prespecified=True)区分。
        from story_lifecycle.knowledge.adapters.opencode import OpencodeAdapter

        a = OpencodeAdapter()
        assert a.prespecified_session_id is False
        assert a.name == "opencode"
        # 输出驱动捕获用不上(opencode 不在终端吐 sid)
        assert a.make_sid_capturer("S", "design") is None

    def test_bypass_flags_auto(self):
        from story_lifecycle.knowledge.adapters.opencode import OpencodeAdapter

        assert OpencodeAdapter().bypass_flags() == ["--auto"]

    def test_headless_uses_run_subcommand(self):
        # opencode run —— 无 TUI 的脚本模式,可当 consult reviewer。
        from story_lifecycle.knowledge.adapters.opencode import OpencodeAdapter

        cmd = OpencodeAdapter().headless_launch_cmd("anthropic/claude-sonnet", "")
        assert cmd[1] == "run"
        assert "--auto" in cmd
        assert "anthropic/claude-sonnet" in cmd

    def test_capture_sid_post_exit_picks_newest_in_window(self, tmp_path, monkeypatch):
        """文件扫描捕获:在 spawn 时间窗内,按 cwd 反查 projectID,取最新 session.id。"""
        from story_lifecycle.knowledge.adapters.opencode import OpencodeAdapter

        storage = tmp_path / "storage"
        proj_dir = storage / "project"
        sess_dir = storage / "session" / "proj-1"
        proj_dir.mkdir(parents=True)
        sess_dir.mkdir(parents=True)
        # project.json 记录 cwd → projectID
        (proj_dir / "proj-1.json").write_text(
            json.dumps({"id": "proj-1", "directory": str(tmp_path)}), encoding="utf-8"
        )
        # 旧会话(spawn 之前,created 早于 since)→ 应被过滤
        (sess_dir / "ses_old.json").write_text(
            json.dumps({"id": "ses_old", "time": {"created": "2026-01-01T00:00:00+00:00"}}),
            encoding="utf-8",
        )
        # 本次会话(spawn 之后)
        (sess_dir / "ses_new.json").write_text(
            json.dumps({"id": "ses_new", "time": {"created": "2026-07-25T10:00:00+00:00"}}),
            encoding="utf-8",
        )
        monkeypatch.setenv("OPENCODE_DATA_DIR", str(tmp_path))

        a = OpencodeAdapter()
        captured = a.capture_sid_post_exit(
            "S", "design", cwd=str(tmp_path), since_ts="2026-07-01T00:00:00+00:00"
        )
        assert captured == "ses_new"

    def test_capture_sid_no_storage_returns_none(self, tmp_path, monkeypatch):
        from story_lifecycle.knowledge.adapters.opencode import OpencodeAdapter

        monkeypatch.setenv("OPENCODE_DATA_DIR", str(tmp_path / "nope"))
        assert OpencodeAdapter().capture_sid_post_exit("S", "design", cwd=str(tmp_path)) is None


class TestGetAdapter:
    def test_builtin_claude(self):
        adapter = get_adapter("claude")
        assert isinstance(adapter, BaseAdapter)

    def test_builtin_opencode(self):
        adapter = get_adapter("opencode")
        assert isinstance(adapter, BaseAdapter)
        assert adapter.name == "opencode"

    def test_builtin_claude_case_insensitive(self):
        adapter = get_adapter("Claude")
        assert isinstance(adapter, BaseAdapter)

    def test_unknown_raises(self):
        with pytest.raises(ValueError, match="Unknown CLI adapter"):
            get_adapter("nonexistent")

    def test_config_driven_adapter(self, tmp_path):
        config_file = tmp_path / "adapters.yaml"
        config_file.write_text(
            "aider:\n  launch_cmd: 'aider --model {model}'\n  inject_method: stdin\n",
            encoding="utf-8",
        )
        with patch("story_lifecycle.knowledge.adapters.shell._CONFIG_PATH", config_file):
            adapter = get_adapter("aider")
            assert isinstance(adapter, ShellAdapter)
            assert adapter.launch_cmd("gpt-4") == "aider --model gpt-4"

    def test_config_not_found_raises(self, tmp_path):
        missing = tmp_path / "nonexistent.yaml"
        with patch("story_lifecycle.knowledge.adapters.shell._CONFIG_PATH", missing):
            with pytest.raises(ValueError, match="Unknown CLI adapter"):
                get_adapter("aider")


class TestWriteAnchor:
    """I2: adapter.write_anchor writes a story<->session anchor to
    <workspace>/.story/runs/<story_key>/anchors.jsonl for miner.link."""

    def test_claude_write_anchor_appends_jsonl(self, tmp_path, monkeypatch):
        adapter = ClaudeAdapter()
        monkeypatch.chdir(tmp_path)
        path = adapter.write_anchor("hello", "STORY-1065518", "design")
        assert path is not None
        assert os.path.basename(path) == "anchors.jsonl"
        with open(path, encoding="utf-8") as fh:
            lines = [json.loads(l) for l in fh if l.strip()]
        assert len(lines) == 1
        a = lines[0]
        assert a["story_key"] == "STORY-1065518"
        assert a["stage"] == "design"
        assert a["adapter"] == "claude"
        assert "cwd" in a and a["cwd"]
        assert "T" in a["ts"]  # iso with time component
        assert len(a["prompt_hash"]) == 16

    def test_write_anchor_explicit_workspace(self, tmp_path):
        adapter = CodexAdapter()
        ws = tmp_path / "ws"
        ws.mkdir()
        path = adapter.write_anchor(
            "p", "S1", "build", cwd=str(ws), workspace=str(ws)
        )
        runs_dir = ws / ".story" / "runs" / "S1"
        assert runs_dir.is_dir()
        with open(path, encoding="utf-8") as fh:
            a = json.loads(fh.read().strip())
        assert a["story_key"] == "S1"
        assert a["adapter"] == "codex"

    def test_inject_prompt_still_returns_none_but_writes_anchor(
        self, tmp_path, monkeypatch
    ):
        # 核心约束：不改 inject_prompt 返回值（claude 仍 None / paste），
        # 但锚点已被追加写。
        adapter = ClaudeAdapter()
        monkeypatch.chdir(tmp_path)
        result = adapter.inject_prompt("some prompt", "KEY", "verify")
        assert result is None
        anchor_file = tmp_path / ".story" / "runs" / "KEY" / "anchors.jsonl"
        assert anchor_file.exists()

    def test_write_anchor_multiple_lines_append(self, tmp_path, monkeypatch):
        adapter = ShellAdapter(config={"inject_method": "stdin"}, name="aider")
        monkeypatch.chdir(tmp_path)
        adapter.write_anchor("p1", "K", "design")
        adapter.write_anchor("p2", "K", "build")
        anchor_file = tmp_path / ".story" / "runs" / "K" / "anchors.jsonl"
        with open(anchor_file, encoding="utf-8") as fh:
            lines = [l for l in fh if l.strip()]
        assert len(lines) == 2
        assert json.loads(lines[0])["stage"] == "design"
        assert json.loads(lines[1])["stage"] == "build"

    def test_write_anchor_does_not_raise_on_bad_path(self, tmp_path):
        # best-effort: 写失败返回 None，不抛异常（不破坏 inject 核心）
        adapter = ClaudeAdapter()
        # workspace 指向一个无法创建的路径（只读/不存在盘符模拟）
        result = adapter.write_anchor(
            "p", "K", "design", workspace="Z:/no/such/nonexistent/xyz"
        )
        # 可能成功也可能 None（取决于系统），关键是不能抛异常
        assert result is None or result.endswith("anchors.jsonl")

