"""Tests for llm_call: prompt/response/reasoning 正文落库 + llm_trace 关联。

覆盖审计链路:思考过程(reasoning_content)、prompt 正文、response 正文现在随每次
LLM 调用进 llm_call 表,外键挂到 llm_trace(id)。主表 llm_trace 保持轻(只指标)。
"""

from story_lifecycle.infra.db import models as db


def test_log_llm_trace_returns_id():
    """log_llm_trace 返回新行 id(供 log_llm_call 外键挂接)。"""
    trace_id = db.log_llm_trace(
        story_key="S1",
        operation="llm_client",
        model="deepseek-v4-pro",
        prompt_tokens=10,
        completion_tokens=20,
        total_tokens=30,
        duration_ms=123,
    )
    assert isinstance(trace_id, int)
    assert trace_id > 0


def test_llm_call_logs_prompt_response_reasoning():
    """三类正文(prompt/response/reasoning)写入 llm_call 并能 JOIN 读回。"""
    db.create_story("S1", "s1", "")
    trace_id = db.log_llm_trace(story_key="S1", operation="llm_client", model="m")
    db.log_llm_call(
        trace_id,
        prompt_text="[{\"role\":\"user\",\"content\":\"hi\"}]",
        response_text="answer",
        reasoning_text="let me think...",
        tool_calls_json="[]",
    )

    rows = db.get_story_llm_calls("S1")
    assert len(rows) == 1
    row = rows[0]
    assert row["prompt_text"] == "[{\"role\":\"user\",\"content\":\"hi\"}]"
    assert row["response_text"] == "answer"
    assert row["reasoning_text"] == "let me think..."
    assert row["model"] == "m"  # JOIN 自 llm_trace
    assert row["prompt_tokens"] == 0  # 指标列也在(未传则默认 0)


def test_llm_call_cascade_on_trace_delete():
    """删 llm_trace 行后,关联的 llm_call 行随之消失(ON DELETE CASCADE)。"""
    db.create_story("S2", "s2", "")
    trace_id = db.log_llm_trace(story_key="S2", operation="llm_client", model="m")
    db.log_llm_call(trace_id, prompt_text="p", response_text="r")

    # 删除前能读到
    assert len(db.get_story_llm_calls("S2")) == 1

    # 直接删 llm_trace 行,FK CASCADE 应连带删 llm_call
    with db._db() as conn:
        conn.execute("DELETE FROM llm_trace WHERE id = ?", (trace_id,))

    assert db.get_story_llm_calls("S2") == []


def test_get_story_llm_calls_isolated_by_story_key():
    """不同 story 的正文互不串。"""
    db.create_story("A", "a", "")
    db.create_story("B", "b", "")
    ta = db.log_llm_trace(story_key="A", operation="llm_client", model="m")
    tb = db.log_llm_trace(story_key="B", operation="llm_client", model="m")
    db.log_llm_call(ta, response_text="from-A")
    db.log_llm_call(tb, response_text="from-B")

    a_rows = db.get_story_llm_calls("A")
    b_rows = db.get_story_llm_calls("B")
    assert {r["response_text"] for r in a_rows} == {"from-A"}
    assert {r["response_text"] for r in b_rows} == {"from-B"}
