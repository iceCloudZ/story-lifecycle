"""Shared paths for story evidence artifacts."""

from __future__ import annotations

import re
from pathlib import Path


class UnsafePathError(ValueError):
    """Raised when a story_key / patch_id or resolved path escapes its container.

    Defense against path-traversal: a malicious ``story_key`` like ``../../etc``
    used to be concatenated directly into filesystem paths (and even paired with
    ``shutil.rmtree``), allowing arbitrary read/write/delete outside the
    workspace. All external-facing entry points must sanitize via
    :func:`safe_segment` and build paths via :func:`safe_story_path`.
    """


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
    # Whitelist word chars, dot, hyphen, underscore; replace others with "-".
    # Strip only trailing/leading hyphens and underscores — keep dots so that
    # legitimate dotfile-style dirs like ".story" / ".git" survive (a leading
    # dot is harmless once path separators and ".." are already excluded).
    cleaned = re.sub(r"[^\w.-]+", "-", value, flags=re.UNICODE).strip("-_")
    # Drop a trailing "." that would make this a "current dir" reference,
    # but keep internal/leading dots (e.g. ".story", "v1.2").
    cleaned = cleaned.rstrip(".")
    # Preserve a single leading dot for dotfile dirs, but reject pure "." / ".."
    # (those are handled explicitly in safe_segment below).
    return cleaned or "story"


def safe_segment(value: str) -> str:
    """Sanitize a single external path segment (story_key / patch_id / filename).

    Whitelist ``\\w.-``, drop everything else, refuse values that still encode a
    traversal attempt after cleaning. Use this at every trust boundary where a
    string coming from API/CLI/DB is about to be concatenated into a path.

    Raises :class:`UnsafePathError` if the cleaned value still contains a path
    separator or parent-reference (defensive — the regex should already strip
    them, but we double-check so a future regex change can't silently regress).
    """
    cleaned = _safe_segment(value)
    if "/" in cleaned or "\\" in cleaned or cleaned in {"..", "."}:
        raise UnsafePathError(f"refusing unsafe path segment: {value!r}")
    return cleaned


def safe_story_path(base: str | Path, *segments: str) -> Path:
    """Build ``base / seg1 / seg2 / ...`` guaranteeing the result stays in base.

    Mixes two defenses:
    1. Every segment is whitelisted via :func:`safe_segment` (collapses ``../``,
       separators, shell metachars). This means a tainted story_key of
       ``../../etc`` becomes a harmless ``etc`` directory *under* base.
    2. As a belt-and-suspenders blast shield, the final resolved path is
       verified to be relative to ``base`` via :func:`assert_within_workspace`.

    Program-constant directories like ``.story`` pass through safely: only
    disallowed characters are stripped (the leading dot of a literal ``.story``
    is preserved because the whitelist already permits ``.``).

    Use this wherever code previously wrote ``Path(ws) / ".story" / story_key``.
    """
    base_path = Path(base)
    if not segments:
        return base_path
    # Sanitize each segment for traversal/separator chars, then join.
    cleaned = [safe_segment(seg) for seg in segments]
    result = base_path.joinpath(*cleaned)
    # Final blast shield: even if a future sanitizer regression let something
    # slip, refuse to return a path that escapes the base.
    try:
        result.resolve().relative_to(base_path.resolve())
    except ValueError as exc:
        raise UnsafePathError(
            f"safe_story_path result escapes base: {result!r} not under {base_path!r}"
        ) from exc
    return result


def assert_within_workspace(path: str | Path, workspace: str | Path) -> None:
    """Assert ``path`` resolves to a location inside ``workspace``.

    Used as a blast-shield before destructive operations (``shutil.rmtree``,
    bulk deletes): even if a path was built from a tainted story_key, refuse to
    touch anything that escapes the workspace root. Raises
    :class:`UnsafePathError` if the resolved ``path`` is not relative to the
    resolved ``workspace``.
    """
    resolved = Path(path).resolve()
    root = Path(workspace).resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise UnsafePathError(
            f"refusing operation outside workspace: {path!r} resolves to "
            f"{resolved} which is not under workspace {root}"
        ) from exc
