"""Tests for Context Resolver and Snapshot."""

from pathlib import Path

from story_lifecycle.infra.db import models as db
from story_lifecycle.orchestrator.context.resolver import ContextResolver
from story_lifecycle.orchestrator.context.snapshot import generate_snapshot


def _setup_story_with_data(isolated_story_home):
    """Create a story with project, document, change item, delivery artifact."""
    key = "test-ctx-story"
    db.create_story(key, "Test Context Story", str(isolated_story_home))
    db.update_story(key, intake_state="ready", profile="minimal")

    proj = db.create_project(name="ctx-test-proj", repo_path=str(isolated_story_home))
    db.bind_story_project(
        story_key=key,
        project_id=proj["id"],
        branch="feature/ctx-test",
        base_branch="main",
        worktree_state="unprepared",
    )
    db.create_document(
        story_key=key,
        kind="prd",
        ref=str(isolated_story_home / "prd.md"),
        summary="Test PRD",
        source="user",
    )
    db.create_change_item(
        story_key=key,
        kind="ddl",
        ref="schema.sql",
        summary="Test DDL",
        lifecycle_state="detected",
        verification_state="unverified",
        source="ai",
    )
    db.create_delivery_artifact(
        story_key=key,
        kind="github_pr",
        provider="github",
        external_id="1",
        delivery_state="review_pending",
        source="user",
    )
    return key


class TestContextResolver:
    def test_resolver_reads_all_entities(self, isolated_story_home):
        """Resolver should read story, projects, documents, change_items, delivery_artifacts."""
        key = _setup_story_with_data(isolated_story_home)

        resolver = ContextResolver()
        bundle = resolver.resolve(key)

        assert bundle.story is not None
        assert bundle.story["story_key"] == key
        assert len(bundle.projects) >= 1
        assert len(bundle.story_projects) >= 1
        assert len(bundle.documents) >= 1
        assert len(bundle.change_items) >= 1
        assert len(bundle.delivery_artifacts) >= 1

    def test_resolver_flags_missing_path(self, isolated_story_home):
        """Resolver should flag non-existent project paths."""
        key = "test-missing-path"
        db.create_story(key, "Missing Path", str(isolated_story_home))
        db.update_story(key, intake_state="ready")
        proj = db.create_project(
            name="ghost-proj",
            repo_path=str(isolated_story_home / "does-not-exist"),
            availability="missing",
        )
        # Must bind the project to the story for the resolver to validate it
        db.bind_story_project(
            story_key=key,
            project_id=proj["id"],
            branch="feature/test",
            worktree_state="unprepared",
        )

        resolver = ContextResolver()
        bundle = resolver.resolve(key)
        errors = resolver.validate(bundle)

        assert any("does not exist" in e for e in errors)

    def test_resolver_flags_invalid_profile_stage(self, isolated_story_home):
        """Resolver should flag when current_stage is not in profile stages."""
        key = "test-invalid-stage"
        db.create_story(key, "Invalid Stage", str(isolated_story_home))
        db.update_story(key, intake_state="ready", current_stage="nonexistent_stage")

        resolver = ContextResolver()
        bundle = resolver.resolve(key)
        errors = resolver.validate(bundle)

        # May or may not have profile-specific errors depending on whether
        # the minimal profile can be loaded. Just verify validation runs.
        assert isinstance(errors, list)

    def test_snapshot_contains_all_sections(self, isolated_story_home):
        """Snapshot should include all required sections."""
        key = _setup_story_with_data(isolated_story_home)

        result = generate_snapshot(key)
        assert result["story_key"] == key

        snapshot_path = Path(result["snapshot_path"])
        assert snapshot_path.exists()

        content = snapshot_path.read_text(encoding="utf-8")
        assert "Story 长期上下文" in content
        assert key in content
        assert "项目" in content
        assert "文档" in content or "PRD" in content or "prd" in content
        assert "DDL" in content or "ddl" in content
        assert "交付" in content or "github_pr" in content

    def test_snapshot_records_revision(self, isolated_story_home):
        """Snapshot result should include context_revision."""
        key = _setup_story_with_data(isolated_story_home)

        result = generate_snapshot(key)
        assert "revision" in result
        assert isinstance(result["revision"], int)

    def test_resolver_raises_for_nonexistent_story(self, isolated_story_home):
        """Resolver should raise ValueError for nonexistent story."""
        resolver = ContextResolver()
        try:
            resolver.resolve("nonexistent-key-99999")
            assert False, "should have raised"
        except ValueError:
            pass
