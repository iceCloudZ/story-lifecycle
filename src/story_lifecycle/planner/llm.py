"""Shared LLM helper for planner modules — OpenAI-compatible chat completion."""

from __future__ import annotations

import json
import logging
import os
import re

import httpx

log = logging.getLogger(__name__)


def _api_config() -> tuple[str, str, str]:
    return (
        os.environ.get("STORY_LLM_API_KEY", ""),
        os.environ.get("STORY_LLM_BASE_URL", "https://api.deepseek.com"),
        os.environ.get("STORY_LLM_MODEL", "deepseek-v4-pro"),
    )


def _extract_json(text: str) -> str | None:
    pairs = {"{": "}", "[": "]"}
    # Find the first opener character outside strings
    in_string = False
    escape_next = False
    first_pos = None
    for i, ch in enumerate(text):
        if escape_next:
            escape_next = False
            continue
        if ch == "\\" and in_string:
            escape_next = True
            continue
        if ch == '"':
            in_string = not in_string
            continue
        if not in_string and ch in pairs:
            first_pos = i
            break
    if first_pos is None:
        return None

    opener = text[first_pos]
    closer = pairs[opener]
    depth = 0
    in_string = False
    escape_next = False
    for i in range(first_pos, len(text)):
        ch = text[i]
        if escape_next:
            escape_next = False
            continue
        if ch == "\\" and in_string:
            escape_next = True
            continue
        if ch == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if ch == opener:
            depth += 1
        elif ch == closer:
            depth -= 1
            if depth == 0:
                return text[first_pos : i + 1]
    return None


def _parse_json_response(content: str) -> dict | list | None:
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        pass
    m = re.search(r"```(?:json)?\s*\n(.*?)\n\s*```", content, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            pass
    extracted = _extract_json(content)
    if extracted:
        try:
            return json.loads(extracted)
        except json.JSONDecodeError:
            pass
    return None


def call_llm(
    prompt: str,
    *,
    system: str = "",
    temperature: float = 0.1,
    max_tokens: int | None = None,
) -> str:
    """Call LLM and return the text response."""
    api_key, base_url, model = _api_config()
    if not api_key:
        raise RuntimeError("LLM API key not configured. Run 'story setup' first.")

    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    body = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
    }
    if max_tokens is not None:
        body["max_tokens"] = max_tokens

    resp = httpx.post(
        f"{base_url}/v1/chat/completions",
        headers={"Authorization": f"Bearer {api_key}"},
        json=body,
        timeout=120,
    )
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"]


def call_llm_json(
    prompt: str, *, system: str = "", temperature: float = 0.1
) -> dict | list | None:
    """Call LLM and parse the response as JSON."""
    content = call_llm(prompt, system=system, temperature=temperature)
    result = _parse_json_response(content)
    if result is None:
        log.warning("LLM response could not be parsed as JSON: %s", content[:200])
    return result
