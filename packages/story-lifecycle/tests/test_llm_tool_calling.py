"""Tests for LLMClient.invoke_with_tools — function calling support."""

from unittest.mock import patch

import pytest

from story_lifecycle.infra.llm_client import LLMClient


def _mock_response(tool_calls=None, content=""):
    """Build a mock httpx response body with tool_calls."""
    message = {"role": "assistant", "content": content}
    if tool_calls:
        message["tool_calls"] = tool_calls
    return {
        "choices": [{"message": message}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30},
    }


@pytest.fixture
def client():
    return LLMClient(
        api_key="test-key", base_url="http://localhost:9999", model="test-model"
    )


class TestInvokeWithTools:
    def test_returns_tool_calls(self, client):
        raw_calls = [
            {
                "id": "call_001",
                "type": "function",
                "function": {
                    "name": "plan_step",
                    "arguments": '{"adapter": "claude", "stage": "design", "focus": "test"}',
                },
            }
        ]
        with patch.object(client, "_request") as mock_req:
            mock_req.return_value = _mock_response(tool_calls=raw_calls)
            result = client.invoke_with_tools(
                [{"role": "user", "content": "plan"}],
                [
                    {
                        "type": "function",
                        "function": {"name": "plan_step", "parameters": {}},
                    }
                ],
            )

        assert len(result["tool_calls"]) == 1
        tc = result["tool_calls"][0]
        assert tc["id"] == "call_001"
        assert tc["function"]["name"] == "plan_step"
        # arguments should be parsed from string to dict
        assert tc["function"]["arguments"] == {
            "adapter": "claude",
            "stage": "design",
            "focus": "test",
        }

    def test_arguments_string_parsed_to_dict(self, client):
        raw_calls = [
            {
                "id": "call_002",
                "type": "function",
                "function": {
                    "name": "skip_stage",
                    "arguments": '{"reason": "not needed"}',
                },
            }
        ]
        with patch.object(client, "_request") as mock_req:
            mock_req.return_value = _mock_response(tool_calls=raw_calls)
            result = client.invoke_with_tools([{"role": "user", "content": "plan"}], [])

        args = result["tool_calls"][0]["function"]["arguments"]
        assert isinstance(args, dict)
        assert args["reason"] == "not needed"

    def test_no_tool_calls_returns_empty_list(self, client):
        with patch.object(client, "_request") as mock_req:
            mock_req.return_value = _mock_response(content="I'm done planning")
            result = client.invoke_with_tools([{"role": "user", "content": "plan"}], [])

        assert result["tool_calls"] == []
        assert result["content"] == "I'm done planning"

    def test_multiple_tool_calls(self, client):
        raw_calls = [
            {
                "id": "call_a",
                "type": "function",
                "function": {
                    "name": "plan_step",
                    "arguments": '{"adapter":"claude","stage":"design","focus":"x"}',
                },
            },
            {
                "id": "call_b",
                "type": "function",
                "function": {
                    "name": "skip_stage",
                    "arguments": '{"reason":"skip test","stage":"test"}',
                },
            },
        ]
        with patch.object(client, "_request") as mock_req:
            mock_req.return_value = _mock_response(tool_calls=raw_calls)
            result = client.invoke_with_tools([{"role": "user", "content": "plan"}], [])

        assert len(result["tool_calls"]) == 2
        assert result["tool_calls"][0]["function"]["name"] == "plan_step"
        assert result["tool_calls"][1]["function"]["name"] == "skip_stage"

    def test_request_includes_tools_and_tool_choice(self, client):
        tools = [{"type": "function", "function": {"name": "test", "parameters": {}}}]
        with patch.object(client, "_request") as mock_req:
            mock_req.return_value = _mock_response(content="ok")
            client.invoke_with_tools(
                [{"role": "user", "content": "go"}],
                tools,
                tool_choice={"type": "function", "function": {"name": "test"}},
            )

        body = mock_req.call_args[0][0]
        assert body["tools"] == tools
        assert body["tool_choice"] == {"type": "function", "function": {"name": "test"}}

    def test_max_tokens_forwarded(self, client):
        with patch.object(client, "_request") as mock_req:
            mock_req.return_value = _mock_response(content="ok")
            client.invoke_with_tools(
                [{"role": "user", "content": "x"}], [], max_tokens=100
            )

        body = mock_req.call_args[0][0]
        assert body["max_tokens"] == 100

    def test_invalid_arguments_json_kept_as_string(self, client):
        raw_calls = [
            {
                "id": "call_003",
                "type": "function",
                "function": {"name": "test", "arguments": "not-valid-json{"},
            }
        ]
        with patch.object(client, "_request") as mock_req:
            mock_req.return_value = _mock_response(tool_calls=raw_calls)
            result = client.invoke_with_tools([{"role": "user", "content": "x"}], [])

        # Invalid JSON should be kept as-is (string)
        assert result["tool_calls"][0]["function"]["arguments"] == "not-valid-json{"
