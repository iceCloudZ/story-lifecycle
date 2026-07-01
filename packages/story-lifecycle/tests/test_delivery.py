"""Tests for delivery artifacts module."""

import pytest

from story_lifecycle.db import models as db
from story_lifecycle.orchestrator.service.delivery import (
    register_delivery,
    update_delivery_state,
    record_review,
    can_cleanup_worktree,
)


class TestDeliveryArtifacts:
    def test_register_github_pr(self, isolated_story_home):
        """Register a GitHub PR delivery artifact."""
        key = "test-pr-story"
        db.create_story(key, "Test PR Story", str(isolated_story_home))
        db.update_story(key, intake_state="ready")

        artifact = register_delivery(
            story_key=key,
            kind="github_pr",
            provider="github",
            external_id="123",
            url="https://github.com/org/repo/pull/123",
            source_branch="feature/test",
            target_branch="main",
            delivery_state="review_pending",
            source="user",
            evidence_ref="https://github.com/org/repo/pull/123",
        )

        assert artifact is not None
        assert artifact["kind"] == "github_pr"
        assert artifact["external_id"] == "123"
        assert artifact["delivery_state"] == "review_pending"

    def test_local_merge_requires_merge_commit(self, isolated_story_home):
        """local_merge kind must have merge_commit and evidence_ref."""
        key = "test-merge-story"
        db.create_story(key, "Test Merge Story", str(isolated_story_home))
        db.update_story(key, intake_state="ready")

        # Missing merge_commit should raise
        with pytest.raises(ValueError, match="merge_commit"):
            register_delivery(
                story_key=key,
                kind="local_merge",
                source="user",
            )

        # Missing evidence_ref should raise
        with pytest.raises(ValueError, match="evidence_ref"):
            register_delivery(
                story_key=key,
                kind="local_merge",
                merge_commit="abc123",
                source="user",
            )

        # With both, should succeed
        artifact = register_delivery(
            story_key=key,
            kind="local_merge",
            merge_commit="abc123",
            evidence_ref="git log screenshot",
            source="user",
        )
        assert artifact is not None
        assert artifact["kind"] == "local_merge"
        assert artifact["merge_commit"] == "abc123"

    def test_ai_cannot_abandon_delivery(self, isolated_story_home):
        """AI (source='ai') is forbidden from setting delivery_state='abandoned'."""
        key = "test-ai-abandon"
        db.create_story(key, "Test AI Abandon", str(isolated_story_home))
        db.update_story(key, intake_state="ready")

        artifact = register_delivery(
            story_key=key,
            kind="local_merge",
            merge_commit="def456",
            evidence_ref="test evidence",
            delivery_state="review_pending",
            source="ai",
        )

        with pytest.raises(PermissionError, match="AI cannot abandon"):
            update_delivery_state(artifact["id"], new_state="abandoned", source="ai")

        # Verify state was NOT changed
        current = db.get_delivery_artifact(artifact["id"])
        assert current["delivery_state"] == "review_pending"

    def test_user_can_abandon_delivery(self, isolated_story_home):
        """User (source='user') can set delivery_state='abandoned'."""
        key = "test-user-abandon"
        db.create_story(key, "Test User Abandon", str(isolated_story_home))
        db.update_story(key, intake_state="ready")

        artifact = register_delivery(
            story_key=key,
            kind="local_merge",
            merge_commit="ghi789",
            evidence_ref="test evidence",
            delivery_state="review_pending",
            source="user",
        )

        updated = update_delivery_state(
            artifact["id"], new_state="abandoned", source="user"
        )
        assert updated["delivery_state"] == "abandoned"

    def test_review_recording(self, isolated_story_home):
        """Record a review conclusion for a delivery artifact."""
        key = "test-review"
        db.create_story(key, "Test Review", str(isolated_story_home))
        db.update_story(key, intake_state="ready")

        artifact = register_delivery(
            story_key=key,
            kind="github_pr",
            provider="github",
            external_id="456",
            url="https://github.com/org/repo/pull/456",
            delivery_state="review_pending",
            source="user",
        )

        updated = record_review(
            artifact["id"],
            review_state="approved",
            summary="LGTM, approved after 2 rounds of review",
        )
        assert updated["review_state"] == "approved"
        assert "LGTM" in updated["review_summary"]

        # waived is also valid
        updated2 = record_review(
            artifact["id"],
            review_state="waived",
            summary="Trivial change, waived",
        )
        assert updated2["review_state"] == "waived"

    def test_cleanup_gate_blocks_unmerged(self, isolated_story_home):
        """can_cleanup_worktree should block when artifacts are not merged/abandoned."""
        key = "test-cleanup-gate"
        db.create_story(key, "Test Cleanup Gate", str(isolated_story_home))
        db.update_story(key, intake_state="ready")

        # No artifacts — should allow cleanup
        can, reason = can_cleanup_worktree(key)
        assert can

        # Add an artifact in review_pending — should block
        artifact = register_delivery(
            story_key=key,
            kind="github_pr",
            provider="github",
            external_id="789",
            delivery_state="review_pending",
            source="user",
        )

        can, reason = can_cleanup_worktree(key)
        assert not can
        assert str(artifact["id"]) in reason

        # Mark as merged — should allow
        update_delivery_state(artifact["id"], "merged", source="user")
        can, reason = can_cleanup_worktree(key)
        assert can

        # Add an abandoned artifact alongside merged — still allow
        _artifact2 = register_delivery(
            story_key=key,
            kind="local_merge",
            merge_commit="jkl012",
            evidence_ref="test",
            delivery_state="abandoned",
            source="user",
        )
        can, reason = can_cleanup_worktree(key)
        assert can
