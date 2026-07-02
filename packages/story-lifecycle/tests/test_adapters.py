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


class TestGetAdapter:
    def test_builtin_claude(self):
        adapter = get_adapter("claude")
        assert isinstance(adapter, BaseAdapter)

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

