"""Shared paths for story evidence artifacts."""

from __future__ import annotations

import re
from pathlib import Path


def story_numeric_id(story_key: str) -> str:
    """Return the last numeric component of a story key, or the key itself."""
    matches = re.findall(r"\d+", story_key or "")
    return matches[-1] if matches else _safe_segment(story_key or "story")


def story_short_slug(title: str, fallback: str = "需求") -> str:
    """Build a compact Chinese/ASCII slug for evidence directory names."""
    cleaned = re.sub(r"[^\w\u4e00-\u9fff]+", "", title or "", flags=re.UNICODE)
    cleaned = cleaned.strip("_")
    if not cleaned:
        cleaned = fallback
    return cleaned[:12]


def story_evidence_root(workspace: str | Path) -> Path:
    """Infer the workspace-level ``story/`` evidence directory.

    For a monorepo-like workspace such as ``D:/hc-all/hc-config``, prefer the
    parent that owns ``.agents`` or ``AGENTS.md``. For standalone projects, keep
    evidence under the project workspace.
    """
    ws = Path(workspace or ".").resolve()
    candidates = [ws, *ws.parents]
    for candidate in candidates:
        if (candidate / ".agents").exists() or (candidate / "AGENTS.md").exists():
            return candidate / "story"
    return ws / "story"


def story_evidence_dir(workspace: str | Path, story_key: str, title: str = "") -> Path:
    sid = story_numeric_id(story_key)
    slug = story_short_slug(title)
    return story_evidence_root(workspace) / f"{sid}-{slug}"


def story_prd_path(workspace: str | Path, story_key: str, title: str = "") -> Path:
    return story_evidence_dir(workspace, story_key, title) / "PRD.md"


def _safe_segment(value: str) -> str:
    cleaned = re.sub(r"[^\w.-]+", "-", value, flags=re.UNICODE).strip("-._")
    return cleaned or "story"
