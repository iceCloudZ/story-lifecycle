"""Shared section builders for prompt injection.

Single source of truth for the knowledge / quality / transcript sections that
two prompt builders consume:

- ``_build_cli_prompt`` (planner.py) — full-auto live path (agent-mode).
- ``_render_prompt`` (nodes/prompt_renderer.py) — semi-auto dry-run / template
  substitution path.

Each helper returns the **raw section content** (the same text the underlying
``context_providers`` / ``quality`` functions return), or ``""`` on any failure
or when there is nothing to inject. Callers own their own wrapping (newlines,
markdown headers, stage gating) so the two paths keep their existing formatting
verbatim — this module only de-duplicates the *fetch + failsafe* logic.

These helpers intentionally never raise: prompt rendering must never be blocked
by a provider or DB error.
"""

from __future__ import annotations

from .. import context_providers


def build_knowledge_section(story_key: str, workspace: str, stage: str) -> str:
    """Return mined knowledge context for this story/stage, or ``""``.

    Wraps ``context_providers.get_knowledge_context`` with a failsafe so prompt
    rendering is never blocked. The returned text is the provider's raw markdown
    (already includes its own ``##`` header); ``""`` means nothing to inject.
    """
    try:
        ctx = context_providers.get_knowledge_context(story_key, workspace, stage)
    except Exception:  # noqa: BLE001 — never block prompt rendering
        return ""
    return ctx or ""


def build_quality_section(story_key: str, stage: str) -> str:
    """Return the compact Quality Checklist text for this story/stage, or ``""``.

    Wraps ``quality.build_quality_checklist`` with a failsafe. The returned text
    is the checklist's raw markdown (already includes its own ``## Quality
    Checklist`` header); ``""`` means nothing to inject.

    Stage gating (e.g. only inject on ``verify``) is the caller's responsibility
    so this helper serves both the template path (where the checklist slot only
    exists in ``verify.md``) and the CLI path (which gates with
    ``if stage == "verify"``) without one's semantics leaking into the other.
    """
    try:
        from .quality import build_quality_checklist

        return build_quality_checklist(story_key, stage) or ""
    except Exception:  # noqa: BLE001 — never block prompt rendering
        return ""


def build_transcript_section(story_key: str, workspace: str, stage: str) -> str:
    """Return historical transcript context for this story/stage, or ``""``.

    Wraps ``context_providers.get_transcript_context`` with a failsafe. The
    returned text is the provider's raw markdown; ``""`` means nothing to inject.
    """
    try:
        ctx = context_providers.get_transcript_context(story_key, workspace, stage)
    except Exception:  # noqa: BLE001 — never block prompt rendering
        return ""
    return ctx or ""


__all__ = [
    "build_knowledge_section",
    "build_quality_section",
    "build_transcript_section",
]
