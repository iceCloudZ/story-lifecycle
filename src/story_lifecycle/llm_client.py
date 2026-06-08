"""Unified LLM client — thin wrapper over OpenAI-compatible chat completion API.

Replaces duplicated httpx + JSON parsing code across 7 files.
Supports DeepSeek, Qwen, Zhipu, and any OpenAI-compatible endpoint.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from typing import TypeVar, Type

import httpx
from pydantic import BaseModel, ValidationError

log = logging.getLogger("story-lifecycle.llm")

T = TypeVar("T", bound=BaseModel)


def _extract_json_object(text: str) -> str | None:
    """Extract first complete JSON object via bracket counting (string-aware)."""
    pairs = {"{": "}", "[": "]"}
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


class LLMClient:
    """Thin wrapper over OpenAI-compatible API. Zero LangChain dependency."""

    def __init__(
        self,
        api_key: str = "",
        base_url: str = "",
        model: str = "",
    ):
        self.api_key = api_key or os.environ.get("STORY_LLM_API_KEY", "")
        self.base_url = base_url or os.environ.get(
            "STORY_LLM_BASE_URL", "https://api.deepseek.com"
        )
        self.model = model or os.environ.get("STORY_LLM_MODEL", "deepseek-v4-pro")

    def invoke(
        self,
        prompt: str,
        *,
        system: str = "",
        temperature: float = 0.1,
        timeout: int = 90,
        max_tokens: int | None = None,
    ) -> str:
        """Call LLM, return text content."""
        body = self._build_body(
            prompt, system=system, temperature=temperature, max_tokens=max_tokens
        )
        resp_body = self._request(body, timeout=timeout)
        return self._extract_content(resp_body)

    def invoke_json(
        self,
        prompt: str,
        *,
        system: str = "",
        temperature: float = 0.1,
        timeout: int = 90,
        max_tokens: int | None = None,
    ) -> dict:
        """Call LLM, parse response as JSON dict."""
        content = self.invoke(
            prompt,
            system=system,
            temperature=temperature,
            timeout=timeout,
            max_tokens=max_tokens,
        )
        result = self._parse_json(content)
        if result is None:
            raise ValueError(f"Cannot parse LLM response as JSON: {content[:200]}")
        return result

    def invoke_structured(
        self,
        prompt: str,
        schema: Type[T],
        *,
        system: str = "",
        temperature: float = 0.1,
        timeout: int = 90,
        max_tokens: int | None = None,
    ) -> T:
        """Call LLM, parse and validate against a Pydantic model.

        Falls back to manual field extraction if strict validation fails.
        """
        content = self.invoke(
            prompt,
            system=system,
            temperature=temperature,
            timeout=timeout,
            max_tokens=max_tokens,
        )
        data = self._parse_json(content)
        if data is None:
            raise ValueError(f"Cannot parse LLM response as JSON: {content[:200]}")
        try:
            return schema.model_validate(data)
        except ValidationError:
            # Best-effort: coerce with partial data
            return schema.model_construct(
                **{k: v for k, v in data.items() if k in schema.model_fields}
            )

    def stream(
        self,
        prompt: str,
        *,
        on_chunk=None,
        system: str = "",
        temperature: float = 0.1,
        timeout: int = 90,
    ) -> str:
        """Stream LLM response with chunk callback. Returns full text."""
        body = self._build_body(prompt, system=system, temperature=temperature)
        body["stream"] = True

        full: list[str] = []
        with httpx.stream(
            "POST",
            f"{self.base_url}/v1/chat/completions",
            headers={"Authorization": f"Bearer {self.api_key}"},
            json=body,
            timeout=timeout,
        ) as resp:
            resp.raise_for_status()
            for raw_line in resp.iter_lines():
                line = raw_line.decode() if isinstance(raw_line, bytes) else raw_line
                if not line.startswith("data: "):
                    continue
                data = line[6:]
                if data == "[DONE]":
                    break
                try:
                    chunk = json.loads(data)
                    delta = chunk["choices"][0]["delta"].get("content", "")
                    if delta:
                        full.append(delta)
                        if on_chunk:
                            on_chunk(delta)
                except (json.JSONDecodeError, KeyError):
                    pass
        return "".join(full)

    # ── internal ──

    def _build_body(
        self,
        prompt: str,
        *,
        system: str = "",
        temperature: float = 0.1,
        max_tokens: int | None = None,
    ) -> dict:
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        body: dict = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
        }
        if max_tokens is not None:
            body["max_tokens"] = max_tokens
        return body

    def _request(self, body: dict, *, timeout: int = 90) -> dict:
        if not self.api_key:
            raise RuntimeError("LLM API key not configured. Run 'story setup' first.")

        t0 = time.monotonic()
        try:
            resp = httpx.post(
                f"{self.base_url}/v1/chat/completions",
                headers={"Authorization": f"Bearer {self.api_key}"},
                json=body,
                timeout=timeout,
            )
            resp.raise_for_status()
            result = resp.json()
            self._trace(result.get("usage", {}), int((time.monotonic() - t0) * 1000))
            return result
        except Exception as exc:
            self._trace({}, int((time.monotonic() - t0) * 1000), error=str(exc))
            raise

    @staticmethod
    def _extract_content(body: dict) -> str:
        msg = body["choices"][0]["message"]
        content = msg.get("content", "") or msg.get("reasoning_content", "")
        if not content.strip():
            raise RuntimeError("LLM returned empty content")
        return content

    @staticmethod
    def _parse_json(content: str) -> dict | None:
        """Robust JSON parsing: direct → markdown fence → bracket counting."""
        try:
            return json.loads(content)
        except (json.JSONDecodeError, TypeError):
            pass
        m = re.search(r"```(?:json)?\s*\n(.*?)\n\s*```", content, re.DOTALL)
        if m:
            try:
                return json.loads(m.group(1))
            except (json.JSONDecodeError, TypeError):
                pass
        extracted = _extract_json_object(content)
        if extracted:
            try:
                return json.loads(extracted)
            except (json.JSONDecodeError, TypeError):
                pass
        return None

    @staticmethod
    def _trace(usage: dict, duration_ms: int, error: str = ""):
        try:
            from .db.models import log_llm_trace

            log_llm_trace(
                story_key="",
                stage="",
                operation="llm_client",
                model="",
                prompt_tokens=usage.get("prompt_tokens", 0),
                completion_tokens=usage.get("completion_tokens", 0),
                total_tokens=usage.get("total_tokens", 0),
                duration_ms=duration_ms,
                success=not bool(error),
                error=error,
            )
        except Exception:
            pass


# ── module-level singleton ──

_client: LLMClient | None = None


def get_llm() -> LLMClient:
    global _client
    if _client is None:
        _client = LLMClient()
    return _client
