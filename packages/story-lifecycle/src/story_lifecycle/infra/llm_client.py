"""Unified LLM client — thin wrapper over OpenAI-compatible chat completion API.

Replaces duplicated httpx + JSON parsing code across 7 files.
Supports DeepSeek, Qwen, Zhipu, and any OpenAI-compatible endpoint.
"""

from __future__ import annotations

import contextvars
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

# Propagate the current story key from orchestrators down to the low-level
# LLM client so token usage can be attributed to the right story.
CURRENT_STORY_KEY: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "current_story_key", default=None
)


def set_current_story_key(story_key: str) -> contextvars.Token:
    """Set the story key for the current execution context.

    Returns a token that can be passed to reset_current_story_key().
    """
    return CURRENT_STORY_KEY.set(story_key)


def reset_current_story_key(token: contextvars.Token) -> None:
    """Reset the story key context variable using a token."""
    CURRENT_STORY_KEY.reset(token)


class story_key_context:
    """Context manager that sets the current story key for LLM tracing."""

    def __init__(self, story_key: str):
        self.story_key = story_key
        self._token: contextvars.Token | None = None

    def __enter__(self):
        self._token = set_current_story_key(self.story_key)
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self._token is not None:
            reset_current_story_key(self._token)
        return False


def with_story_key(arg_name: str = "story_key", *, from_state: bool = False):
    """Decorator that sets the current story key for the duration of a function.

    Args:
        arg_name: Keyword argument name to read story_key from (when not from_state).
        from_state: If True, read story_key from the first positional arg dict.
    """
    import functools

    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            if from_state:
                state = args[0] if args else kwargs.get("state", {})
                story_key = (
                    state.get("story_key", "") if isinstance(state, dict) else ""
                )
            else:
                story_key = kwargs.get(arg_name, "")
                if not story_key and args:
                    story_key = args[0]
                story_key = story_key or ""
            with story_key_context(str(story_key)):
                return func(*args, **kwargs)

        return wrapper

    return decorator


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


# Models known to support vision/multimodal input (OpenAI-compatible).
VISION_MODELS: set[str] = {
    "gpt-4o",
    "gpt-4o-mini",
    "gpt-4-turbo",
    "gpt-4-vision-preview",
    "claude-sonnet-4-20250514",
    "claude-opus-4-20250514",
    "claude-haiku-4-5-20251001",
    "claude-3-5-sonnet",
    "claude-3-opus",
    "claude-3-sonnet",
    "claude-3-haiku",
    "qwen-vl-max",
    "qwen-vl-plus",
    "qwen2-vl",
    "qwen2-5-vl",
    "glm-4v",
    "glm-4v-plus",
    "glm-4-flash",
    "moonshot-v1-32k-vision-preview",
    "moonshot-v1-128k-vision-preview",
    "kimi-k2.5",
    "kimi-k2",
    "kimi-for-coding",
}


def _is_vision_model(model: str) -> bool:
    """Best-effort check whether a model name indicates vision support."""
    lowered = model.lower()
    return any(vm in lowered for vm in VISION_MODELS)


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

    def _chat_completions_url(self) -> str:
        """Return the chat completions URL, tolerating base_urls with /v1 suffix."""
        base = self.base_url.rstrip("/")
        if base.endswith("/v1"):
            return f"{base}/chat/completions"
        return f"{base}/v1/chat/completions"

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

    def invoke_vision(
        self,
        prompt: str,
        images: list[str],
        *,
        system: str = "",
        temperature: float = 0.1,
        timeout: int = 120,
        max_tokens: int | None = None,
    ) -> str:
        """Call a vision-capable LLM with text + image URLs or base64 data URLs.

        Args:
            prompt: Text prompt.
            images: List of image URLs or base64 data URLs
                (e.g. "https://..." or "data:image/png;base64,...").
        Returns:
            Text content from the model.
        Raises:
            RuntimeError: if the configured model does not appear vision-capable.
        """
        if not _is_vision_model(self.model):
            log.warning(
                "Model %s is not in the known-vision list; vision call may fail.",
                self.model,
            )
        body = self._build_multimodal_body(
            prompt,
            images,
            system=system,
            temperature=temperature,
            max_tokens=max_tokens,
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
            raise ValueError(
                f"Cannot parse LLM response as JSON. First 500 chars: {content[:500]!r}"
            )
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
            raise ValueError(
                f"Cannot parse LLM response as JSON. First 500 chars: {content[:500]!r}"
            )
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
            self._chat_completions_url(),
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

    def invoke_with_tools(
        self,
        messages: list[dict],
        tools: list[dict],
        *,
        tool_choice: str | dict = "auto",
        temperature: float = 0.1,
        timeout: int = 90,
        max_tokens: int | None = None,
    ) -> dict:
        """Call LLM with function calling tools. Returns parsed tool_calls.

        Args:
            messages: Full conversation history (supports multi-turn).
            tools: OpenAI function calling format tool definitions.
            tool_choice: "auto" | "none" | {"type":"function","function":{"name":"..."}}
            temperature: Sampling temperature.
            timeout: Request timeout in seconds.
            max_tokens: Max tokens for response.

        Returns:
            {
                "message": {"role": "assistant", "content": "...", "tool_calls": [...]},
                "tool_calls": [
                    {"id": "call_xxx", "type": "function",
                     "function": {"name": "plan_step", "arguments": "{...}"}}
                ],
                "content": "..."  # text content if any
            }
        """
        body: dict = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "tools": tools,
            "tool_choice": tool_choice,
        }
        if max_tokens is not None:
            body["max_tokens"] = max_tokens

        resp_body = self._request(body, timeout=timeout)
        msg = resp_body["choices"][0]["message"]

        tool_calls = msg.get("tool_calls") or []
        # Normalize tool_calls: ensure arguments are parsed from string
        normalized = []
        for tc in tool_calls:
            fn = tc.get("function", {})
            args_str = fn.get("arguments", "{}")
            if isinstance(args_str, str):
                try:
                    args_str = json.loads(args_str)
                except json.JSONDecodeError:
                    pass
            normalized.append(
                {
                    "id": tc.get("id", ""),
                    "type": tc.get("type", "function"),
                    "function": {
                        "name": fn.get("name", ""),
                        "arguments": args_str,
                    },
                }
            )

        return {
            "message": msg,
            "tool_calls": normalized,
            "content": msg.get("content", "") or "",
        }

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

    def _build_multimodal_body(
        self,
        prompt: str,
        images: list[str],
        *,
        system: str = "",
        temperature: float = 0.1,
        max_tokens: int | None = None,
    ) -> dict:
        content: list[dict] = [{"type": "text", "text": prompt}]
        for img in images:
            content.append({"type": "image_url", "image_url": {"url": img}})

        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": content})

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
                self._chat_completions_url(),
                headers={"Authorization": f"Bearer {self.api_key}"},
                json=body,
                timeout=timeout,
            )
            resp.raise_for_status()
            result = resp.json()
            self._trace(
                result.get("usage", {}),
                int((time.monotonic() - t0) * 1000),
                model=self.model,
            )
            return result
        except Exception as exc:
            self._trace(
                {},
                int((time.monotonic() - t0) * 1000),
                model=self.model,
                error=str(exc),
            )
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
        """Robust JSON parsing: direct → markdown fence → bracket counting.

        Logs the full content on failure so callers can diagnose malformed
        LLM output without re-running requests.
        """
        last_error = None
        try:
            return json.loads(content)
        except (json.JSONDecodeError, TypeError) as exc:
            last_error = exc
        m = re.search(r"```(?:json)?\s*\n(.*?)\n\s*```", content, re.DOTALL)
        if m:
            try:
                return json.loads(m.group(1))
            except (json.JSONDecodeError, TypeError) as exc:
                last_error = exc
        extracted = _extract_json_object(content)
        if extracted:
            try:
                return json.loads(extracted)
            except (json.JSONDecodeError, TypeError) as exc:
                last_error = exc
        log.warning(
            "LLM response JSON parse failed: %s\n--- content ---\n%s\n--- end ---",
            last_error,
            content,
        )
        return None

    @staticmethod
    def _trace(usage: dict, duration_ms: int, *, model: str = "", error: str = ""):
        try:
            from .db.models import log_llm_trace

            story_key = CURRENT_STORY_KEY.get() or ""
            log_llm_trace(
                story_key=story_key,
                stage="",
                operation="llm_client",
                model=model,
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
_vision_client: LLMClient | None = None


def get_llm() -> LLMClient:
    global _client
    if _client is None:
        _client = LLMClient()
    return _client


def get_vision_llm() -> LLMClient | None:
    """Return a vision-capable LLM client.

    Priority:
    1. Dedicated vision config (STORY_VISION_API_KEY / BASE_URL / MODEL).
    2. Fallback to the main LLM config if its model appears vision-capable.
    3. None if no vision-capable config is available.
    """
    global _vision_client
    if _vision_client is not None:
        return _vision_client

    vision_key = os.environ.get("STORY_VISION_API_KEY", "")
    vision_base = os.environ.get("STORY_VISION_BASE_URL", "")
    vision_model = os.environ.get("STORY_VISION_MODEL", "")
    if vision_key:
        _vision_client = LLMClient(
            api_key=vision_key,
            base_url=vision_base or os.environ.get("STORY_LLM_BASE_URL", ""),
            model=vision_model or os.environ.get("STORY_LLM_MODEL", ""),
        )
        return _vision_client

    main = get_llm()
    if _is_vision_model(main.model):
        return main
    return None
