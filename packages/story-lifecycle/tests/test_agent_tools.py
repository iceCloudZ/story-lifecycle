"""Tests for agent_tools.py — validate tool definitions."""

import json

from story_lifecycle.orchestrator.engine.agent_tools import ORCHESTRATOR_TOOLS


class TestToolSchema:
    """Validate ORCHESTRATOR_TOOLS structure and JSON schema compliance."""

    def test_is_valid_json(self):
        """Tool definitions must be serializable."""
        serialized = json.dumps(ORCHESTRATOR_TOOLS)
        parsed = json.loads(serialized)
        assert parsed == ORCHESTRATOR_TOOLS

    def test_has_six_tools(self):
        assert len(ORCHESTRATOR_TOOLS) == 6

    def test_tool_names(self):
        names = [t["function"]["name"] for t in ORCHESTRATOR_TOOLS]
        assert names == [
            "plan_step",
            "launch_cli",
            "check_done_file",
            "skip_stage",
            "mark_complete",
            "mark_failed",
        ]

    def test_all_tools_have_required_fields(self):
        for tool in ORCHESTRATOR_TOOLS:
            fn = tool["function"]
            assert fn["name"], "Tool must have a name"
            assert fn["description"], "Tool must have a description"
            params = fn["parameters"]
            assert params["type"] == "object"
            assert "properties" in params
            assert "required" in params

    def test_adapter_enum_values(self):
        """Adapter parameters must only accept claude/codex."""
        for tool in ORCHESTRATOR_TOOLS:
            props = tool["function"]["parameters"]["properties"]
            if "adapter" in props:
                assert props["adapter"]["type"] == "string"
                assert props["adapter"]["enum"] == ["claude", "codex"]

    def test_plan_step_required_fields(self):
        tool = next(
            t for t in ORCHESTRATOR_TOOLS if t["function"]["name"] == "plan_step"
        )
        required = tool["function"]["parameters"]["required"]
        assert set(required) == {"adapter", "stage", "focus"}

    def test_launch_cli_required_fields(self):
        tool = next(
            t for t in ORCHESTRATOR_TOOLS if t["function"]["name"] == "launch_cli"
        )
        required = tool["function"]["parameters"]["required"]
        assert set(required) == {"adapter", "stage", "focus"}

    def test_check_done_file_required_fields(self):
        tool = next(
            t for t in ORCHESTRATOR_TOOLS if t["function"]["name"] == "check_done_file"
        )
        required = tool["function"]["parameters"]["required"]
        assert "path" in required

    def test_skip_stage_required_fields(self):
        tool = next(
            t for t in ORCHESTRATOR_TOOLS if t["function"]["name"] == "skip_stage"
        )
        required = tool["function"]["parameters"]["required"]
        assert set(required) == {"reason", "stage"}

    def test_mark_complete_required_fields(self):
        tool = next(
            t for t in ORCHESTRATOR_TOOLS if t["function"]["name"] == "mark_complete"
        )
        required = tool["function"]["parameters"]["required"]
        assert required == ["summary"]

    def test_mark_failed_required_fields(self):
        tool = next(
            t for t in ORCHESTRATOR_TOOLS if t["function"]["name"] == "mark_failed"
        )
        required = tool["function"]["parameters"]["required"]
        assert required == ["error"]
