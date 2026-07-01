"""Delivery artifacts — CRUD + state machine + cleanup gate.

Manages code delivery outputs: GitHub PR, GitLab MR, local merge, etc.
Enforces rule: AI cannot set delivery_state="abandoned".
"""

from __future__ import annotations

from ...db import models as db


def register_delivery(
    story_key: str,
    kind: str,
    project_id: int | None = None,
    provider: str = "",
    external_id: str = "",
    url: str = "",
    source_branch: str = "",
    target_branch: str = "",
    delivery_state: str = "not_started",
    review_state: str = "not_reviewed",
    merge_commit: str = "",
    review_summary: str = "",
    source: str = "user",
    evidence_ref: str = "",
) -> dict:
    """Register a delivery artifact for a story.

    For kind='local_merge', merge_commit and evidence_ref must be non-empty.
    """
    if not story_key:
        raise ValueError("story_key is required")
    if not kind:
        raise ValueError("kind is required")
    if kind not in ("github_pr", "gitlab_mr", "local_merge", "other"):
        raise ValueError(f"invalid kind: {kind}")

    if kind == "local_merge":
        if not merge_commit:
            raise ValueError("local_merge requires merge_commit")
        if not evidence_ref:
            raise ValueError("local_merge requires evidence_ref")

    return db.create_delivery_artifact(
        story_key=story_key,
        project_id=project_id,
        kind=kind,
        provider=provider,
        external_id=external_id,
        url=url,
        source_branch=source_branch,
        target_branch=target_branch,
        delivery_state=delivery_state,
        review_state=review_state,
        merge_commit=merge_commit,
        review_summary=review_summary,
        source=source,
        evidence_ref=evidence_ref,
    )


def update_delivery_state(
    artifact_id: int, new_state: str, source: str = "user"
) -> dict:
    """Update delivery_state with source guard.

    Rule: source="ai" cannot set new_state="abandoned".
    Only users (source="user") can abandon delivery.
    """
    valid_states = {
        "not_started",
        "preparing",
        "review_pending",
        "approved",
        "merged",
        "abandoned",
    }
    if new_state not in valid_states:
        raise ValueError(f"invalid delivery_state: {new_state}")

    if source == "ai" and new_state == "abandoned":
        raise PermissionError(
            "AI cannot abandon delivery — must be confirmed by user (source='user')"
        )

    db.update_delivery_artifact(artifact_id, delivery_state=new_state)
    return db.get_delivery_artifact(artifact_id)


def record_review(artifact_id: int, review_state: str, summary: str = "") -> dict:
    """Record a review conclusion for a delivery artifact."""
    valid_review_states = {"not_reviewed", "changes_requested", "approved", "waived"}
    if review_state not in valid_review_states:
        raise ValueError(f"invalid review_state: {review_state}")

    db.update_delivery_artifact(
        artifact_id, review_state=review_state, review_summary=summary
    )
    return db.get_delivery_artifact(artifact_id)


def can_cleanup_worktree(story_key: str) -> tuple[bool, str]:
    """Check whether all delivery artifacts for a story are finalized.

    Returns (can_cleanup, reason).
    All artifacts must be in ('merged', 'abandoned') for cleanup to be allowed.
    """
    artifacts = _get_story_delivery_artifacts(story_key)
    if not artifacts:
        return True, "no delivery artifacts to block cleanup"

    non_finalized = [
        a for a in artifacts if a["delivery_state"] not in ("merged", "abandoned")
    ]
    if non_finalized:
        ids = [str(a["id"]) for a in non_finalized]
        return False, f"artifacts not finalized: {', '.join(ids)}"

    return True, "all delivery artifacts finalized"


def _get_story_delivery_artifacts(story_key: str) -> list[dict]:
    """Get all delivery artifacts for a story."""
    from ...db import models as db

    with db._db() as conn:
        rows = conn.execute(
            "SELECT * FROM story_delivery_artifact WHERE story_key = ? ORDER BY id",
            (story_key,),
        ).fetchall()
    return [dict(r) for r in rows]


def get_delivery_artifact(artifact_id: int) -> dict | None:
    """Get a single delivery artifact by id."""
    return db.get_delivery_artifact(artifact_id)


def list_delivery_artifacts(story_key: str) -> list[dict]:
    """List all delivery artifacts for a story."""
    return _get_story_delivery_artifacts(story_key)
