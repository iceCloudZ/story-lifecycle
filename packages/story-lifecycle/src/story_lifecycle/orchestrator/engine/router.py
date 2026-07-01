"""LLM Router — handles unhappy path decisions (retry/skip/fail).

Delegates LLM calls to LLMClient. Uses Pydantic RouteDecision for structured output.
"""

import json
import logging

from ...infra.llm_client import get_llm
from ...infra.schemas import RouteDecision

log = logging.getLogger("story-lifecycle.router")


def route(state: dict, stage_config: dict) -> dict:
    """Decide what to do next.

    Returns:
        {"action": "retry|skip|fail", "reasoning": "...", "provider_override": "..."}
    """
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

    llm = get_llm()
    try:
        result = llm.invoke_structured(
            prompt, RouteDecision, temperature=0.1, timeout=30, max_tokens=200
        )
    except Exception as exc:
        log.warning("LLM router call failed: %s", exc)
        return {"action": "fail", "reasoning": f"LLM error: {type(exc).__name__}"}

    decision = {"action": result.action, "reasoning": result.reasoning}
    if result.provider_override:
        decision["provider_override"] = result.provider_override
    return decision
