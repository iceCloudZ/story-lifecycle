"""Regression tests for invoke_structured parsing robustness.

真实事故 2026-08-20（读取需求 POST /api/intake/preview 两次 502）：
1. deepseek 返回合法 JSON 但非对象（`[]`）→ 旧代码在 _construct_recursive
   兜底里 model_validate(list) 抛 ValidationError，可重试的解析失败直接 502；
2. deepseek 把推理独白当回答输出，两次都不是 JSON → ValueError。
"""

from unittest.mock import patch

import pytest
from pydantic import BaseModel

from story_lifecycle.infra.llm_client import LLMClient


class _Result(BaseModel):
    action: str = ""
    summary: str = ""


@pytest.fixture
def client():
    return LLMClient(
        api_key="test-key", base_url="http://localhost:9999", model="test-model"
    )


def test_non_dict_json_retries_with_correction_then_succeeds(client):
    # 事故#1 形态：第一次返回 []，纠正重试后返回合法对象 → 应当成功而非抛错
    with patch.object(
        client,
        "invoke",
        side_effect=["[]", '{"action": "generated", "summary": "ok"}'],
    ) as mock_invoke:
        result = client.invoke_structured("p", _Result)

    assert result.action == "generated"
    assert mock_invoke.call_count == 2
    # 重试 prompt 带上了纠正提示和上次的错误示范
    retry_prompt = mock_invoke.call_args_list[1].args[0]
    assert "不是合法 JSON" in retry_prompt
    assert "[]" in retry_prompt


def test_non_dict_json_all_attempts_fails_as_value_error(client):
    # 全程返回 [] 时必须以 ValueError（解析失败）收场，
    # 不能冒出 pydantic 的 "Input should be a valid dictionary"
    with patch.object(client, "invoke", return_value="[]"):
        with pytest.raises(ValueError, match="Cannot parse LLM response as a JSON object"):
            client.invoke_structured("p", _Result)


def test_plain_text_reasoning_monologue_fails_as_value_error(client):
    # 事故#2 形态：模型把推理独白当回答输出，通篇没有 JSON
    monologue = "我们需要回答用户。要求只输出 JSON 对象，不要 markdown。（通篇独白）"
    with patch.object(client, "invoke", return_value=monologue):
        with pytest.raises(ValueError, match="Cannot parse LLM response as a JSON object"):
            client.invoke_structured("p", _Result)


def test_valid_dict_response_validates_normally(client):
    with patch.object(client, "invoke", return_value='{"action": "generated"}') as mock_invoke:
        result = client.invoke_structured("p", _Result)

    assert result.action == "generated"
    assert mock_invoke.call_count == 1
