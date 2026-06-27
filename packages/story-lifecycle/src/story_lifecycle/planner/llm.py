"""Shared LLM helper for planner modules — delegates to LLMClient."""

from __future__ import annotations

import logging

from ..llm_client import get_llm

log = logging.getLogger(__name__)


def call_llm(
    prompt: str,
    *,
    system: str = "",
    temperature: float = 0.1,
    max_tokens: int | None = None,
) -> str:
    """Call LLM and return the text response."""
    return get_llm().invoke(
        prompt,
        system=system,
        temperature=temperature,
        max_tokens=max_tokens,
        timeout=120,
    )


def call_llm_json(
    prompt: str, *, system: str = "", temperature: float = 0.1
) -> dict | list | None:
    """Call LLM and parse the response as JSON."""
    try:
        return get_llm().invoke_json(
            prompt, system=system, temperature=temperature, timeout=120
        )
    except ValueError:
        log.warning("LLM response could not be parsed as JSON")
        return None
