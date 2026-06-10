"""TAPD sync service — transform SourceItems into local stories."""

from __future__ import annotations

import logging
from pathlib import Path

from ..db import models as db

log = logging.getLogger(__name__)


def sync_tapd(
    items: list,
    workspace: str = "",
    profile: str = "minimal",
    dry_run: bool = False,
    status_only: bool = False,
) -> dict:
    """Sync TAPD SourceItems into local stories.

    Returns dict with counts: created, updated, skipped, would_create.
    """
    result = {"created": 0, "updated": 0, "skipped": 0, "would_create": 0}
    ws = workspace or str(Path.cwd())

    for item in items:
        existing = db.find_by_source_id(item.source, item.id)

        if dry_run:
            if existing:
                result["updated"] += 1
            else:
                result["would_create"] += 1
            continue

        if existing:
            updates = {}
            if item.title:
                updates["title"] = item.title
            if item.deadline:
                updates["deadline"] = item.deadline
            if item.priority:
                updates["priority"] = item.priority
            if item.owner:
                updates["owner"] = item.owner
            if item.status:
                updates["tapd_status"] = item.status
            url = item.extra.get("url", "")
            if url:
                updates["tapd_url"] = url
            if updates:
                db.update_story(existing["story_key"], **updates)
            result["updated"] += 1
            log.info(f"Updated story for {item.source}:{item.id}")
        elif status_only:
            result["skipped"] += 1
        else:
            # Determine tapd_type
            if item.item_type == "bug":
                tapd_type = "bug"
            elif item.parent_id and item.parent_id != "0":
                tapd_type = "subtask"
            else:
                tapd_type = "story"

            story, _ = db.upsert_story_from_source(
                source_type=item.source,
                source_id=item.id,
                title=item.title,
                workspace=ws,
                profile=profile,
                deadline=item.deadline,
                priority=item.priority,
                owner=item.owner,
                tapd_status=item.status,
                tapd_url=item.extra.get("url", ""),
                tapd_type=tapd_type,
            )
            result["created"] += 1
            log.info(f"Created story {story['story_key']} for {item.source}:{item.id}")

    return result
