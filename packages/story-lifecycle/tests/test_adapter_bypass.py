"""Tests for adapter bypass_flags(0d 权限源头堵)。

codex/kimi 在源头用 CLI flag 堵权限提问(自动批准),claude 走 supervisor
(``--permission-prompt-tool``)不 bypass。flag 列表纯数据,可测;launch 接线环境阻断。
"""

from story_lifecycle.knowledge.adapters.codex import CodexAdapter
from story_lifecycle.knowledge.adapters.claude import ClaudeAdapter
from story_lifecycle.knowledge.adapters.shell import ShellAdapter


def test_codex_bypass_flags_has_full_auto():
    flags = CodexAdapter().bypass_flags()
    assert isinstance(flags, list)
    assert "--full-auto" in flags  # codex 自动批准


def test_claude_bypass_flags_empty():
    """claude 走 --permission-prompt-tool(supervisor 决策),不 bypass。"""
    assert ClaudeAdapter().bypass_flags() == []


def test_shell_bypass_flags_from_config():
    """ShellAdapter(kimi 等)从 adapters.yaml 的 bypass_flags 读;默认空。"""
    assert ShellAdapter({"bypass_flags": ["--auto"]}, name="kimi").bypass_flags() == ["--auto"]
    assert ShellAdapter({}, name="kimi").bypass_flags() == []


def test_base_default_is_empty():
    """未知 adapter / 未配置 → 默认不 bypass(supervisor 兜底)。"""
    from story_lifecycle.knowledge.adapters.base import BaseAdapter

    # BaseAdapter 是 ABC,借 ShellAdapter(无配置)验默认路径
    assert ShellAdapter({}, name="x").bypass_flags() == []
