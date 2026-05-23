"""Tests for ShellAdapter and adapter registry."""

import pytest
from unittest.mock import patch

from story_lifecycle.adapters import get_adapter
from story_lifecycle.adapters.base import BaseAdapter
from story_lifecycle.adapters.shell import ShellAdapter


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
        with patch("story_lifecycle.adapters.shell._CONFIG_PATH", config_file):
            adapter = get_adapter("aider")
            assert isinstance(adapter, ShellAdapter)
            assert adapter.launch_cmd("gpt-4") == "aider --model gpt-4"

    def test_config_not_found_raises(self, tmp_path):
        missing = tmp_path / "nonexistent.yaml"
        with patch("story_lifecycle.adapters.shell._CONFIG_PATH", missing):
            with pytest.raises(ValueError, match="Unknown CLI adapter"):
                get_adapter("aider")
