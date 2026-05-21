"""LLM Router — handles unhappy path decisions (retry/skip/fail).
Phase 1: if-else fallback when no LLM configured.
Phase 2: DeepSeek/OpenAI-compatible API for intelligent routing."""

import json
import os
import logging

log = logging.getLogger("story-lifecycle.router")

# LLM config from env vars
LLM_API_KEY = os.environ.get("STORY_LLM_API_KEY", "")
LLM_BASE_URL = os.environ.get("STORY_LLM_BASE_URL", "https://api.deepseek.com")
LLM_MODEL = os.environ.get("STORY_LLM_MODEL", "deepseek-chat")

_llm_available = bool(LLM_API_KEY)


def llm_is_available() -> bool:
    return _llm_available


def route(state: dict, stage_config: dict) -> dict:
    """Decide what to do next.

    Returns:
        {"action": "retry|skip|fail", "reasoning": "...", "provider_override": "..."}
    """
    if _llm_available:
        return _llm_route(state, stage_config)
    return _rule_route(state, stage_config)


def _rule_route(state: dict, stage_config: dict) -> dict:
    """Phase 1 fallback: pure if-else rules."""
    max_retries = stage_config.get("max_retries", 2)
    exec_count = state.get("execution_count", 0)
    error = state.get("last_error", "")

    if exec_count < max_retries:
        # Auto-switch provider on retry
        current_provider = state.get("context", {}).get("_provider", "")
        allowed = stage_config.get("allowed_providers", [])
        if allowed and current_provider in allowed:
            idx = allowed.index(current_provider)
            next_provider = allowed[(idx + 1) % len(allowed)]
            return {
                "action": "retry",
                "reasoning": f"Retry {exec_count + 1}/{max_retries}. Switching provider to {next_provider}.",
                "provider_override": next_provider,
            }

        return {
            "action": "retry",
            "reasoning": f"Retry {exec_count + 1}/{max_retries}.",
            "provider_override": current_provider if current_provider else None,
        }

    return {
        "action": "fail",
        "reasoning": f"Max retries ({max_retries}) exceeded. Last error: {error}",
    }


def _llm_route(state: dict, stage_config: dict) -> dict:
    """Call LLM to make routing decision."""
    import httpx

    prompt = f"""You are a workflow orchestrator. A development stage has encountered an error.

Story: {state.get('story_key')}
Stage: {state.get('current_stage')}
Stage description: {stage_config.get('description', '')}
Error: {state.get('last_error', 'Unknown')}
Retried: {state.get('execution_count', 0)} times (max: {stage_config.get('max_retries', 2)})
Context: {json.dumps(state.get('context', {}), ensure_ascii=False)}
Available providers: {stage_config.get('allowed_providers', ['default'])}

Decide the next action:
- retry: retry this stage (you may suggest switching to a different provider)
- skip: skip this stage (only if outputs are optional or already done)
- fail: mark as failed and wait for human (only if unrecoverable)

Respond with a JSON object:
{{"action": "retry|skip|fail", "reasoning": "why", "provider_override": "provider_name or null"}}"""

    try:
        resp = httpx.post(
            f"{LLM_BASE_URL}/v1/chat/completions",
            headers={"Authorization": f"Bearer {LLM_API_KEY}"},
            json={
                "model": LLM_MODEL,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.1,
                "max_tokens": 200,
            },
            timeout=30,
        )
        resp.raise_for_status()
        content = resp.json()["choices"][0]["message"]["content"]
        return json.loads(content)
    except Exception as e:
        log.warning(f"LLM route failed, falling back to rules: {e}")
        return _rule_route(state, stage_config)
