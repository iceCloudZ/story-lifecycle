"""design-dimensions section: interactive vs autonomous clarify protocol.

Interactive terminal spawns claude via `claude "query"` WITHOUT --mcp-config,
so it has no `mcp__lifecycle__clarify` tool — the prompt must tell it to ask the
human directly in the terminal instead. The autonomous path (headless -p, with
MCP) keeps the MCP clarify instruction.
"""
from story_lifecycle.orchestrator.engine.prompt_sections import (
    build_design_dimensions_section,
)


def test_interactive_omits_mcp_clarify_and_asks_human_in_terminal(tmp_path):
    s = build_design_dimensions_section("IP-1", str(tmp_path), "design", interactive=True)
    assert "mcp__lifecycle__clarify" not in s  # interactive claude has no MCP tool
    assert "终端" in s  # tell claude to ask the watching human directly


def test_default_keeps_mcp_clarify_for_autonomous_path(tmp_path):
    s = build_design_dimensions_section("IP-1", str(tmp_path), "design")
    assert "mcp__lifecycle__clarify" in s  # headless -p has the MCP tool


def test_interactive_keeps_dimensions_checklist(tmp_path):
    s = build_design_dimensions_section("IP-1", str(tmp_path), "design", interactive=True)
    assert "设计维度 checklist" in s  # only the clarify protocol changes
    assert "brainstorming" in s  # the no-brainstorming rule stays
