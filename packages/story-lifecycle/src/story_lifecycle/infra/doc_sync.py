"""Sync versioned docs between DB (source of truth) and the local .md cache.

The local .md file is a **read-only cache** of the latest version. Code agents
read the .md file (not the DB) — this avoids loading full content into the LLM
context and keeps execution independent of DB availability. A `.meta` sidecar
next to the .md records `{version, hash}` so the execution layer can verify the
cache is current without a DB round-trip.

This module is the ONLY place that writes the local .md for versioned docs.
Humans edit through the web UI (→ DB → ``sync_doc_to_local`` here → .md); AI
stage outputs are registered by ``planner._register_stage_outputs`` (→ DB →
same sync path). The local .md is never hand-edited.
"""

from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path

from .db import models as db
from .story_paths import (
    story_doc_meta_path,
    story_doc_path,
)

log = logging.getLogger("story-lifecycle.doc_sync")


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sync_doc_to_local(
    story_key: str,
    doc_type: str,
    content: str,
    version: int,
    workspace: str,
    title: str = "",
) -> Path:
    """Write the latest DB content to the local .md + .meta cache files.

    Creates parent dirs. Overwrites existing cache. Records local_path back to
    DB so the API can report where the cache lives. Returns the .md path.
    """
    md_path = story_doc_path(workspace, story_key, doc_type, title)
    meta_path = story_doc_meta_path(workspace, story_key, doc_type, title)
    md_path.parent.mkdir(parents=True, exist_ok=True)
    md_path.write_text(content, encoding="utf-8")
    meta = {"version": version, "hash": _sha256(content)}
    meta_path.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    try:
        db.set_story_doc_local_path(story_key, doc_type, str(md_path))
    except Exception as exc:  # best-effort; local_path is informational
        log.debug("set_story_doc_local_path failed (non-fatal): %s", exc)
    return md_path


def verify_local_cache(
    workspace: str, story_key: str, doc_type: str, title: str = ""
) -> bool:
    """Weak check: does the local .md match its .meta hash?

    No DB query — purely local. Used by the execution layer to decide whether
    the cache is usable as-is or needs rebuild from DB.
    """
    md_path = story_doc_path(workspace, story_key, doc_type, title)
    meta_path = story_doc_meta_path(workspace, story_key, doc_type, title)
    if not md_path.exists() or not meta_path.exists():
        return False
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        actual = _sha256(md_path.read_text(encoding="utf-8"))
        return meta.get("hash") == actual
    except (OSError, ValueError, KeyError):
        return False


def rebuild_local_from_db(
    story_key: str,
    doc_type: str,
    workspace: str,
    title: str = "",
) -> Path | None:
    """If the local cache is missing/stale, rebuild it from DB latest_content.

    Returns the rebuilt .md path, or None if the doc doesn't exist in DB (so the
    caller can fall back to the legacy ctx['prd_path']).
    """
    doc = db.get_story_doc(story_key, doc_type)
    if not doc:
        return None
    return sync_doc_to_local(
        story_key,
        doc_type,
        doc["latest_content"],
        int(doc["current_version"]),
        workspace,
        title,
    )


def get_doc_for_execution(
    story_key: str,
    doc_type: str,
    workspace: str,
    title: str = "",
    legacy_path: str = "",
) -> str:
    """Return the .md path for code agents to read.

    Resolution order (designed so execution NEVER blocks on DB):
      1. local .md exists + .meta hash matches → use it (no DB query)
      2. local missing/stale → rebuild from DB latest_content → use it
      3. DB has no such doc → fall back to legacy_path (existing ctx['prd_path']
         behavior, for old stories that predate versioned docs)

    Returns a filesystem path string. The caller injects it into the stage
    prompt; the code agent reads the file itself.
    """
    md_path = story_doc_path(workspace, story_key, doc_type, title)
    # 1. fast path: local cache is current
    if verify_local_cache(workspace, story_key, doc_type, title):
        return str(md_path)
    # 2. cache stale/missing → try rebuild from DB
    rebuilt = rebuild_local_from_db(story_key, doc_type, workspace, title)
    if rebuilt:
        return str(rebuilt)
    # 3. fall back to legacy path (old stories, pre-versioning)
    if legacy_path:
        return legacy_path
    # nothing works — return the canonical path anyway (will just be absent);
    # the stage prompt will note "PRD file: <path>" and the agent will report
    # it's missing, which is the existing behavior for stories without a PRD.
    return str(md_path)
