"""Tests for auto discovery Scanner, Decider, and Handler."""

from story_lifecycle.infra.db import models as db
from story_lifecycle.orchestrator.context.auto_discovery import (
    Scanner,
    Decider,
    Handler,
)


class TestAutoDiscoveryScanner:
    def test_scanner_finds_sql_files(self, tmp_path):
        """Scanner should find SQL files in a project directory."""
        # Create a directory structure with SQL files
        project_dir = tmp_path / "project"
        project_dir.mkdir()
        (project_dir / "schema.sql").write_text("CREATE TABLE test (id INT);")
        migration_dir = project_dir / "migration"
        migration_dir.mkdir()
        (migration_dir / "V1__init.sql").write_text("CREATE TABLE v1 (id INT);")

        scanner = Scanner()
        result = scanner.scan(
            "test-story",
            sp={"worktree_path": str(project_dir), "branch": "main"},
            project={"id": 1, "repo_path": str(project_dir)},
        )

        assert len(result.sql_files) >= 1
        assert any("schema.sql" in f for f in result.sql_files)

    def test_scanner_finds_nacos_refs(self, tmp_path):
        """Scanner should find Nacos Data ID references in config files."""
        project_dir = tmp_path / "nacos-project"
        project_dir.mkdir()
        config = project_dir / "application.yml"
        config.write_text(
            """spring:
  cloud:
    nacos:
      config:
        data-id: hc-order-service.yaml
        group: DEFAULT_GROUP
"""
        )

        scanner = Scanner()
        result = scanner.scan(
            "test-story",
            sp={"worktree_path": str(project_dir), "branch": "main"},
            project={"id": 1, "repo_path": str(project_dir)},
        )

        assert len(result.nacos_refs) >= 1
        assert any("hc-order-service.yaml" in r["data_id"] for r in result.nacos_refs)

    def test_scanner_no_worktree_returns_missing(self, tmp_path):
        """When worktree_path doesn't exist on disk, Scanner must NOT fall back to
        repo_path (would scan the wrong branch). Return errors instead."""
        repo_dir = tmp_path / "repo"
        repo_dir.mkdir()
        (repo_dir / "schema.sql").write_text("CREATE TABLE fallback (id INT);")

        scanner = Scanner()
        result = scanner.scan(
            "test-story",
            sp={"worktree_path": str(tmp_path / "nonexistent-worktree"), "branch": ""},
            project={"id": 1, "repo_path": str(repo_dir)},
        )

        assert not result.fallback_mode
        assert len(result.errors) >= 1
        assert result.sql_files == []  # did not scan repo_path

    def test_scanner_null_worktree_returns_missing(self, tmp_path):
        """worktree_path NULL (unprepared binding) → must not scan, return errors."""
        repo_dir = tmp_path / "repo"
        repo_dir.mkdir()
        (repo_dir / "schema.sql").write_text("CREATE TABLE x (id INT);")

        scanner = Scanner()
        result = scanner.scan(
            "test-story",
            sp={"worktree_path": None, "branch": ""},
            project={"id": 1, "repo_path": str(repo_dir)},
        )

        assert not result.fallback_mode
        assert len(result.errors) >= 1
        assert result.sql_files == []

    def test_decider_merge_detects_new_facts(self, tmp_path):
        """Decider should detect new documents and change items not already in DB."""
        project_dir = tmp_path / "proj"
        project_dir.mkdir()
        (project_dir / "prd").mkdir()
        (project_dir / "prd" / "feature.md").write_text("# Feature PRD")
        (project_dir / "migration.sql").write_text("ALTER TABLE foo ADD bar INT;")

        scanner = Scanner()
        scan_result = scanner.scan(
            "test-story",
            sp={"worktree_path": str(project_dir), "branch": ""},
            project={"id": 1, "repo_path": str(project_dir)},
        )

        # Current context is empty
        decider = Decider()
        mutation = decider.merge([], [], scan_result)

        # Should have discovered the PRD and SQL
        assert len(mutation.new_documents) >= 1
        assert any("prd" in d["kind"] for d in mutation.new_documents)
        assert len(mutation.new_change_items) >= 1
        assert any("ddl" in ci["kind"] for ci in mutation.new_change_items)

    def test_handler_applies_and_bumps_revision(self, isolated_story_home):
        """Handler should apply mutations and bump context_revision."""
        key = "test-handler-story"
        db.create_story(key, "Handler Test", str(isolated_story_home))
        db.update_story(key, intake_state="ready")

        mutation = __import__(
            "story_lifecycle.orchestrator.context.auto_discovery",
            fromlist=["ContextMutation"],
        ).ContextMutation(
            new_documents=[
                {
                    "kind": "prd",
                    "ref": "test_prd.md",
                    "summary": "Test PRD",
                    "source": "ai",
                    "evidence_ref": "scan",
                    "verification_state": "unverified",
                }
            ],
            new_change_items=[
                {
                    "kind": "ddl",
                    "ref": "test.sql",
                    "summary": "Test DDL",
                    "lifecycle_state": "detected",
                    "verification_state": "unverified",
                    "source": "ai",
                    "evidence_ref": "scan",
                }
            ],
        )

        handler = Handler()
        old_rev = db.get_context_revision(key)
        new_rev = handler.apply(key, mutation)

        assert new_rev > old_rev

        # Verify documents and change items were created
        resolver = __import__(
            "story_lifecycle.orchestrator.context.resolver",
            fromlist=["ContextResolver"],
        ).ContextResolver()
        bundle = resolver.resolve(key)
        assert len(bundle.documents) >= 1
        assert len(bundle.change_items) >= 1
