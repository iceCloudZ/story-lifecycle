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
