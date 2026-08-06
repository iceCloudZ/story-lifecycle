"""traces — LLM trace + token 用量（设计15 阶段B 拆分自 models.py）。"""

from __future__ import annotations

import json
import os

from .connection import _db
def log_llm_trace(
    *,
    story_key: str = "",
    stage: str = "",
    operation: str,
    model: str,
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    total_tokens: int = 0,
    duration_ms: int = 0,
    success: bool = True,
    error: str = "",
) -> int:
    """Record an LLM call trace with token usage. Returns the new row id."""
    with _db() as conn:
        cur = conn.execute(
            """INSERT INTO llm_trace (story_key, stage, operation, model,
               prompt_tokens, completion_tokens, total_tokens,
               duration_ms, success, error)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               RETURNING id""",
            (
                story_key,
                stage,
                operation,
                model,
                prompt_tokens,
                completion_tokens,
                total_tokens,
                duration_ms,
                1 if success else 0,
                error,
            ),
        )
        return cur.fetchone()[0]


def log_llm_call(
    trace_id: int,
    *,
    prompt_text: str = "",
    response_text: str = "",
    reasoning_text: str = "",
    tool_calls_json: str = "",
) -> int:
    """Record the prompt/response/reasoning body of an LLM call.

    Linked to ``llm_trace`` via ``trace_id`` (ON DELETE CASCADE). Returns the
    new row id. Callers should keep ``log_llm_trace`` + ``log_llm_call`` paired.
    """
    with _db() as conn:
        cur = conn.execute(
            """INSERT INTO llm_call
               (trace_id, prompt_text, response_text, reasoning_text, tool_calls_json)
               VALUES (?, ?, ?, ?, ?)
               RETURNING id""",
            (trace_id, prompt_text, response_text, reasoning_text, tool_calls_json),
        )
        return cur.fetchone()[0]


def get_story_llm_calls(story_key: str) -> list[dict]:
    """Return prompt/response/reasoning bodies for a story, ordered by call id.

    JOIN llm_call ↔ llm_trace on trace_id, filter by story_key. Use this for
    auditing what was asked/answered/thought across an orchestration run.
    """
    with _db() as conn:
        rows = conn.execute(
            """SELECT lc.id, lc.trace_id, lc.prompt_text, lc.response_text,
                      lc.reasoning_text, lc.tool_calls_json, lc.created_at,
                      lt.stage, lt.operation, lt.model, lt.prompt_tokens,
                      lt.completion_tokens, lt.total_tokens, lt.duration_ms,
                      lt.success, lt.error
               FROM llm_call lc
               JOIN llm_trace lt ON lt.id = lc.trace_id
               WHERE lt.story_key = ?
               ORDER BY lc.id""",
            (story_key,),
        ).fetchall()
    return [dict(r) for r in rows]


def _pricing_for_model(model: str) -> dict[str, float]:
    """Return CNY pricing per 1M tokens for a model name.

    Falls back to longest prefix match, then default. Env var
    STORY_TOKEN_PRICING_JSON can override or extend the table.
    """
    import os

    pricing = dict(MODEL_PRICING_CNY)
    env_json = os.environ.get("STORY_TOKEN_PRICING_JSON", "")
    if env_json:
        try:
            pricing.update(json.loads(env_json))
        except Exception:
            pass

    normalized = (model or "").lower().strip()
    if normalized in pricing:
        return pricing[normalized]

    # Longest prefix match
    best = None
    best_len = 0
    for key in pricing:
        if key == "default":
            continue
        if normalized.startswith(key.lower()) and len(key) > best_len:
            best = pricing[key]
            best_len = len(key)

    return best if best else pricing["default"]


def get_story_token_usage(story_key: str) -> dict:
    """Aggregate LLM token usage and estimated cost for a story.

    Returns:
        {
            "prompt_tokens": int,
            "completion_tokens": int,
            "total_tokens": int,
            "calls": int,
            "cost_cny": float,
            "by_stage": dict[str, int],
            "by_model": dict[str, int],
        }
    """
    with _db() as conn:
        rows = conn.execute(
            """SELECT stage, model, prompt_tokens, completion_tokens, total_tokens
               FROM llm_trace
               WHERE story_key = ?""",
            (story_key,),
        ).fetchall()

    total_prompt = 0
    total_completion = 0
    total_tokens = 0
    calls = 0
    by_stage: dict[str, int] = {}
    by_model: dict[str, int] = {}
    cost_cny = 0.0

    for r in rows:
        model = r["model"] or ""
        stage = r["stage"] or "unknown"
        prompt = r["prompt_tokens"] or 0
        completion = r["completion_tokens"] or 0
        total = r["total_tokens"] or 0

        total_prompt += prompt
        total_completion += completion
        total_tokens += total
        calls += 1

        by_stage[stage] = by_stage.get(stage, 0) + total
        by_model[model] = by_model.get(model, 0) + total

        p = _pricing_for_model(model)
        cost_cny += (prompt * p["input"] + completion * p["output"]) / 1_000_000

    return {
        "prompt_tokens": total_prompt,
        "completion_tokens": total_completion,
        "total_tokens": total_tokens,
        "calls": calls,
        "cost_cny": round(cost_cny, 4),
        "by_stage": by_stage,
        "by_model": by_model,
    }


MODEL_PRICING_CNY: dict[str, dict[str, float]] = {
    "default": {"input": 5.0, "output": 5.0},
    "deepseek-v3": {"input": 2.0, "output": 8.0},
    "deepseek-chat": {"input": 1.0, "output": 5.0},
    "deepseek-reasoner": {"input": 4.0, "output": 16.0},
    "deepseek-v4-pro": {"input": 2.0, "output": 8.0},
    "kimi-k2.5": {"input": 10.0, "output": 30.0},
    "kimi-k2": {"input": 10.0, "output": 30.0},
    "kimi-for-coding": {"input": 10.0, "output": 30.0},
    "moonshot-v1-8k": {"input": 6.0, "output": 6.0},
    "moonshot-v1-32k": {"input": 12.0, "output": 12.0},
    "moonshot-v1-128k": {"input": 24.0, "output": 24.0},
    "qwen-max": {"input": 20.0, "output": 60.0},
    "qwen-plus": {"input": 8.0, "output": 20.0},
    "qwen-turbo": {"input": 2.0, "output": 6.0},
    "qwen-coder-plus": {"input": 8.0, "output": 20.0},
    "gpt-4o": {"input": 35.0, "output": 105.0},
    "gpt-4o-mini": {"input": 1.5, "output": 6.0},
    "claude-3-5-sonnet": {"input": 21.0, "output": 105.0},
    "claude-3-5-sonnet-20241022": {"input": 21.0, "output": 105.0},
}


