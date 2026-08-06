"""Tolerant JSON parsing for .done files (⑤ infra).

Moved here from `orchestrator/nodes/json_helpers.py` (ISS-006) so that
`knowledge/` no longer imports from the orchestration layer — fixing a
layering inversion where a long-term-memory module reached into the
orchestration engine for a utility.

Pure functions: only imports `json`/`re`/`pathlib`. Safe to depend on from
any layer.
"""

import json
import re
from pathlib import Path


def _extract_json_object(text: str) -> str | None:
    """Extract first complete JSON object via bracket counting (string-aware).

    字符串感知：跳过大括号/方括号内的字符串字面量及其转义，避免把 JSON 内容里
    的 ``{"a": "}"}`` 当结构括号。唯一定义点（llm_client 从这里 import）。
    """
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


def robust_json_parse(filepath: Path) -> dict:
    """Parse .done JSON with tolerance for markdown wrapping."""
    raw = filepath.read_text(encoding="utf-8")

    # Strategy 1: direct parse
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass

    # Strategy 2: bracket-counting extraction (handles arbitrary nesting)
    extracted = _extract_json_object(raw)
    if extracted:
        try:
            return json.loads(extracted)
        except json.JSONDecodeError:
            pass

    # Strategy 3: try extracting between ```json fences
    m = re.search(r"```json\s*\n(.*?)\n\s*```", raw, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            pass

    raise ValueError(f"Cannot parse JSON from {filepath}: {raw[:200]}")
