"""Migrate legacy local .md docs (story/{id}-{slug}/*.md) into story_doc DB.

One-shot migration: scans ``<workspace>/story/*/`` for *.md files, infers
story_key from the directory's numeric prefix (e.g. ``1064006-xxx`` →
``1064006``), matches it against ``story.story_key`` in DB, and imports each
file as version 1 (author='migration'). Idempotent: docs that already have a
version are skipped unless --force.

Usage::

    python -m scripts.migrate_docs_to_db --workspace D:/hc-all
    python -m scripts.migrate_docs_to_db --workspace D:/hc-all --force

doc_type mapping: PRD.md → prd, spec.md → spec, plan.md → plan, research.md →
research, test-report.md → test_report; any other ``foo.md`` → ``foo``.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

# story_doc.doc_type derived from filename. Matches the canonical _DOC_FILENAMES
# in story_paths but inverted (filename → doc_type).
_FILE_TO_TYPE: dict[str, str] = {
    "PRD.md": "prd",
    "spec.md": "spec",
    "plan.md": "plan",
    "research.md": "research",
    "test-report.md": "test_report",
    "bugfix-report.md": "bugfix-report",
    "delivery.md": "delivery",
}


def _doc_type_for(filename: str) -> str | None:
    if not filename.endswith(".md"):
        return None
    if filename in _FILE_TO_TYPE:
        return _FILE_TO_TYPE[filename]
    return filename[:-3]  # custom: foo.md → foo


def _numeric_prefix(dirname: str) -> str | None:
    """'1064006-反欺诈直连通过' → '1064006'. Returns None if no leading digits."""
    m = re.match(r"^(\d+)", dirname)
    return m.group(1) if m else None


def migrate(workspace: str, *, force: bool = False) -> int:
    """Run the migration. Returns the count of docs imported."""
    # Late imports so the script is importable without the package on sys.path.
    from story_lifecycle.infra.db import models as db
    from story_lifecycle.infra.doc_sync import sync_doc_to_local

    db.init_db()

    story_root = Path(workspace) / "story"
    if not story_root.is_dir():
        print(f"no story/ dir under {workspace}", file=sys.stderr)
        return 0

    imported = 0
    skipped = 0
    missing_story = 0

    for story_dir in sorted(story_root.iterdir()):
        if not story_dir.is_dir():
            continue
        numeric = _numeric_prefix(story_dir.name)
        if not numeric:
            continue
        # match against DB: try the numeric id directly, then a LIKE on story_key
        row = db.get_story(numeric)
        if not row:
            # some story keys are tapd-<numeric>; try suffix match
            like_rows = db.search_stories_by_key_fragment(numeric) if hasattr(db, "search_stories_by_key_fragment") else []
            if not like_rows:
                # last resort: raw query
                import sqlite3
                from story_lifecycle.infra.db.models import _db
                with _db() as conn:
                    r = conn.execute(
                        "SELECT story_key FROM story WHERE story_key LIKE ? ORDER BY updated_at DESC LIMIT 1",
                        (f"%{numeric}%",),
                    ).fetchone()
                like_rows = [r["story_key"]] if r else []
            if not like_rows:
                missing_story += 1
                continue
            story_key = like_rows[0]
        else:
            story_key = numeric

        for md_file in story_dir.glob("*.md"):
            doc_type = _doc_type_for(md_file.name)
            if not doc_type:
                continue
            # idempotent: skip if already has a version
            if not force:
                existing = db.get_story_doc(story_key, doc_type)
                if existing:
                    skipped += 1
                    continue
            try:
                content = md_file.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            if not content.strip():
                continue
            version = db.upsert_story_doc(
                story_key,
                doc_type,
                content,
                change_reason="初始导入（从本地文件迁移）",
                author="migration",
            )
            # also sync the local cache (re-stamp .meta)
            try:
                story_row = db.get_story(story_key) or {}
                ws = story_row.get("workspace") or workspace
                title = story_row.get("title") or ""
                sync_doc_to_local(story_key, doc_type, content, version, ws, title)
            except Exception:
                pass  # local sync is best-effort
            imported += 1
            print(f"  imported {story_key}/{doc_type} ← {md_file.name} (v{version})")

    print(f"\nDone: imported={imported}, skipped(already migrated)={skipped}, missing_story={missing_story}")
    return imported


def main() -> None:
    ap = argparse.ArgumentParser(description="Migrate local .md docs into story_doc DB")
    ap.add_argument("--workspace", required=True, help="workspace root (e.g. D:/hc-all)")
    ap.add_argument("--force", action="store_true", help="re-import even if doc already has a version")
    args = ap.parse_args()
    migrate(args.workspace, force=args.force)


if __name__ == "__main__":
    main()
