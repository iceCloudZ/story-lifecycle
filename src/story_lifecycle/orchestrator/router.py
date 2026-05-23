"""LLM Router — handles unhappy path decisions (retry/skip/fail).
Requires an LLM API key. Uses OpenAI-compatible API for intelligent routing."""

import json
import os
import logging

log = logging.getLogger("story-lifecycle.router")


def _get_api_key():
    return os.environ.get("STORY_LLM_API_KEY", "")


def _get_base_url():
    return os.environ.get("STORY_LLM_BASE_URL", "https://api.deepseek.com")


def _get_model():
    return os.environ.get("STORY_LLM_MODEL", "deepseek-chat")


def llm_is_available() -> bool:
    return bool(_get_api_key())


def route(state: dict, stage_config: dict) -> dict:
    """Decide what to do next.

    Returns:
        {"action": "retry|skip|fail", "reasoning": "...", "provider_override": "..."}
    """
    if not _get_api_key():
        raise RuntimeError("LLM API key not configured. Run 'story setup' first.")

    return _llm_route(state, stage_config)


def _extract_json_object(text: str) -> str | None:
    """Extract the first complete JSON object using bracket counting."""
    depth = 0
    start = None
    for i, ch in enumerate(text):
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and start is not None:
                return text[start : i + 1]
    return None


def _parse_llm_json(content: str) -> dict:
    """Parse LLM response as JSON with tolerance for truncation and markdown wrapping."""
    # Direct parse
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        pass

    # Extract from markdown code fence
    import re

    m = re.search(r"```(?:json)?\s*\n(.*?)\n\s*```", content, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            pass

    # Bracket-counting extraction (handles truncated output)
    extracted = _extract_json_object(content)
    if extracted:
        try:
            return json.loads(extracted)
        except json.JSONDecodeError:
            pass

    log.warning(f"Failed to parse LLM router response: {content[:200]}")
    return {"action": "fail", "reasoning": "LLM response not valid JSON"}


def _llm_route(state: dict, stage_config: dict) -> dict:
    """Call LLM to make routing decision."""
    import httpx

    prompt = f"""You are a workflow orchestrator. A development stage has encountered an error.

Story: {state.get("story_key")}
Stage: {state.get("current_stage")}
Stage description: {stage_config.get("description", "")}
Error: {state.get("last_error", "Unknown")}
Retried: {state.get("execution_count", 0)} times (max: {stage_config.get("max_retries", 2)})
Context: {json.dumps(state.get("context", {}), ensure_ascii=False)}
Available providers: {stage_config.get("allowed_providers", ["default"])}

Decide the next action:
- retry: retry this stage (you may suggest switching to a different provider)
- skip: skip this stage (only if outputs are optional or already done)
- fail: mark as failed and wait for human (only if unrecoverable)

Respond with a JSON object:
{{"action": "retry|skip|fail", "reasoning": "why", "provider_override": "provider_name or null"}}"""

    resp = httpx.post(
        f"{_get_base_url()}/v1/chat/completions",
        headers={"Authorization": f"Bearer {_get_api_key()}"},
        json={
            "model": _get_model(),
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.1,
            "max_tokens": 200,
        },
        timeout=30,
    )
    resp.raise_for_status()
    content = resp.json()["choices"][0]["message"]["content"]
    return _parse_llm_json(content)
