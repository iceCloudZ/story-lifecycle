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


# ---------------------------------------------------------------------------
# Story spawn-env: env-var names are the single source of truth here.
#
# Producer (spawners via build_story_spawn_env) and consumer
# (artifact_declare._story_ctx reads these to locate the evidence dir by
# title-slug) MUST use the same names — previously planner.py hardcoded the
# strings and STORY_TITLE was missing, so declare() computed slug from empty
# title and fell back to literal "需求". See commit history (STORY_TITLE 漏注入).
# ---------------------------------------------------------------------------
ENV_STORY_KEY = "STORY_KEY"
ENV_STORY_STAGE = "STORY_STAGE"
ENV_STORY_WORKSPACE = "STORY_WORKSPACE"
ENV_STORY_ADAPTER = "STORY_ADAPTER"
ENV_STORY_TITLE = "STORY_TITLE"  # 可选,用于 story evidence 目录命名


def build_story_spawn_env(story: dict, stage: str, adapter_name: str) -> dict:
    """Build the spawn env for a code agent (claude/kimi/opencode/codex).

    Single source for all spawn paths (planner headless + planner PTY +
    api interactive PTY). Layers on ``os.environ`` so the child keeps the
    serve process' environment and adds the five STORY_* vars:

    - STORY_KEY / STORY_STAGE / STORY_WORKSPACE / STORY_ADAPTER: locate the
      story so downstream ``story tool declare`` / ``story consult`` / MCP
      clarify server can resolve it.
    - STORY_TITLE: used by ``artifact_declare._story_ctx`` →
      :func:`story_short_slug` to name the evidence subdir
      (``<key>-<title-slug>/``). Without it the slug falls back to literal
      "需求", drifting from the PRD's subdir which is built with the real
      title (incident 2026-07-28: same story had artifacts across
      ``-事件中心新增提额成功事件`` / ``-需求`` / bare ``story/``).
    """
    import os

    return {
        **os.environ,
        ENV_STORY_KEY: story.get("story_key", ""),
        ENV_STORY_STAGE: stage or "",
        ENV_STORY_WORKSPACE: story.get("workspace", ""),
        ENV_STORY_ADAPTER: adapter_name or "",
        ENV_STORY_TITLE: story.get("title", ""),
    }


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


# Filename convention per doc_type. PRD keeps its historical "PRD.md" name so
# existing code that greps for PRD.md still works; other types use lowercase.
_DOC_FILENAMES: dict[str, str] = {
    "prd": "PRD.md",
    "spec": "spec.md",
    "plan": "plan.md",
    "research": "research.md",
    "test_report": "test-report.md",
    "bugfix-report": "bugfix-report.md",
    "delivery": "delivery.md",
}


def doc_filename(doc_type: str) -> str:
    """Canonical .md filename for a doc_type (prd→PRD.md, custom→{type}.md)."""
    return _DOC_FILENAMES.get(doc_type, f"{doc_type}.md")


def doc_type_for_filename(filename: str) -> str | None:
    """Reverse of :func:`doc_filename` for KNOWN types ('delivery.md'→'delivery').

    Returns None when the basename isn't a canonical doc filename — callers must
    NOT guess a custom type from an arbitrary ``{type}.md`` name (that would
    misclassify ordinary markdown files as docs).
    """
    for dt, fn in _DOC_FILENAMES.items():
        if fn == filename:
            return dt
    return None


# canonical doc_type → alias doc_types(code agent 实际会用的非规范名)。
# 单一真相源:deliverables gate / artifact_check evidence / 文档查看都经此解析,
# 避免 design 落库后 spec gate 显示"暂无产物"(2026-07-28 真实事件:
# tapd-...1066924 设计文档 doc_type=design 但 deliverables 只查 spec)。
# 系统此前在 4 处各写各的副本(artifact_check._DOC_TYPE_FILENAMES /
# planner._STAGE_DOC_KIND / migrate_done / detector._SPEC_DIRS),集中到此处。
DOC_TYPE_ALIASES: dict[str, list[str]] = {
    "spec": ["design"],  # code agent 爱用 design.md 命名(planner prompt 明确叫它别用)
    # test_report / prd / delivery 无已知 doc_type 别名(DB 里就是规范名)。
}


def resolve_doc_type_aliases(doc_type: str) -> list[str]:
    """Return ``[canonical, *aliases]`` lookup candidates for a doc_type.

    Canonical first; for unknown doc_types returns ``[doc_type]`` (single).
    Callers (e.g. deliverables gate) iterate these against ``get_story_doc``
    so a ``design``-registered doc satisfies the ``spec`` deliverable.
    """
    return [doc_type, *DOC_TYPE_ALIASES.get(doc_type, [])]


def story_doc_path(
    workspace: str | Path, story_key: str, doc_type: str, title: str = ""
) -> Path:
    """Path to the local-cache .md file for a versioned doc."""
    return story_evidence_dir(workspace, story_key, title) / doc_filename(doc_type)


def story_doc_meta_path(
    workspace: str | Path, story_key: str, doc_type: str, title: str = ""
) -> Path:
    """Path to the .meta sidecar (version + hash) that lives next to the .md."""
    return story_doc_path(workspace, story_key, doc_type, title).with_name(
        doc_filename(doc_type) + ".meta"
    )


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
