import json

import pytest
import yaml
from pathlib import Path
from click.testing import CliRunner
from story_lifecycle.cli.main import cli
from story_lifecycle.knowledge.templates import load_template
from story_lifecycle.knowledge.paths import (
    knowledge_dir,
    manifest_path,
    product_path,
    search_catalog_path,
    graph_dir,
    graph_json_path,
    scenarios_dir,
    indexes_dir,
    index_by_domain_dir,
    playbooks_dir,
    declarations_dir,
    reviews_dir,
    events_dir,
    cache_dir,
    knowledge_done_file,
    knowledge_context_dir,
)
from story_lifecycle.knowledge.scaffold import scaffold_knowledge_dir
from story_lifecycle.knowledge.bootstrap import render_bootstrap_prompt
from story_lifecycle.knowledge.validator import validate_knowledge_pack
from story_lifecycle.knowledge.stale import check_stale
from story_lifecycle.knowledge.search import search_knowledge


def test_knowledge_dir():
    assert knowledge_dir("/ws") == Path("/ws/.story/knowledge")


def test_manifest_path():
    assert manifest_path("/ws") == Path("/ws/.story/knowledge/manifest.yaml")


def test_product_path():
    assert product_path("/ws") == Path("/ws/.story/knowledge/product.yaml")


def test_search_catalog_path():
    assert search_catalog_path("/ws") == Path("/ws/.story/knowledge/search-catalog.md")


def test_graph_dir():
    assert graph_dir("/ws") == Path("/ws/.story/knowledge/graph")


def test_graph_json_path():
    assert graph_json_path("/ws") == Path(
        "/ws/.story/knowledge/graph/product-context-graph.json"
    )


def test_scenarios_dir():
    assert scenarios_dir("/ws") == Path("/ws/.story/knowledge/scenarios")


def test_indexes_dir():
    assert indexes_dir("/ws") == Path("/ws/.story/knowledge/indexes")


def test_index_by_domain_dir():
    assert index_by_domain_dir("/ws") == Path("/ws/.story/knowledge/indexes/by-domain")


def test_playbooks_dir():
    assert playbooks_dir("/ws") == Path("/ws/.story/knowledge/playbooks")


def test_declarations_dir():
    assert declarations_dir("/ws") == Path("/ws/.story/knowledge/declarations")


def test_reviews_dir():
    assert reviews_dir("/ws") == Path("/ws/.story/knowledge/reviews")


def test_events_dir():
    assert events_dir("/ws") == Path("/ws/.story/knowledge/events")


def test_cache_dir():
    assert cache_dir("/ws") == Path("/ws/.story/knowledge/cache")


def test_knowledge_done_file():
    assert knowledge_done_file("/ws") == Path(
        "/ws/.story/done/PROJECT-KNOWLEDGE-INIT/knowledge_bootstrap.json"
    )


def test_knowledge_context_dir():
    assert knowledge_context_dir("/ws", "STORY-1") == Path(
        "/ws/.story/context/STORY-1/knowledge-context"
    )


class TestScaffold:
    def test_creates_all_dirs(self, tmp_path):
        scaffold_knowledge_dir(tmp_path)
        dirs = [
            "knowledge/scenarios",
            "knowledge/indexes/by-domain",
            "knowledge/graph",
            "knowledge/playbooks",
            "knowledge/declarations",
            "knowledge/reviews",
            "knowledge/events",
            "knowledge/cache",
            "done/PROJECT-KNOWLEDGE-INIT",
        ]
        for d in dirs:
            assert (tmp_path / ".story" / d).is_dir(), f"Missing .story/{d}"

    def test_creates_gitignore(self, tmp_path):
        scaffold_knowledge_dir(tmp_path)
        gi = tmp_path / ".story" / "knowledge" / ".gitignore"
        assert gi.exists()
        content = gi.read_text(encoding="utf-8")
        assert "/indexes/" in content
        assert "/graph/" in content
        assert "/events/" in content
        assert "/cache/" in content
        assert "product.yaml" not in content

    def test_idempotent(self, tmp_path):
        scaffold_knowledge_dir(tmp_path)
        scaffold_knowledge_dir(tmp_path)  # should not raise
        assert (tmp_path / ".story" / "knowledge").is_dir()


class TestTemplates:
    @pytest.mark.parametrize(
        "name",
        [
            "manifest.yaml",
            "product.yaml",
            "search-catalog.md",
            "graph-schema.json",
            "scenario.md",
            "index.md",
        ],
    )
    def test_template_exists_and_nonempty(self, name):
        content = load_template(name)
        assert len(content) > 50, f"{name} is too short"

    def test_manifest_is_valid_yaml(self):
        import yaml

        content = load_template("manifest.yaml")
        data = yaml.safe_load(content)
        assert "version" in data
        assert "product" in data

    def test_graph_schema_is_valid_json(self):
        import json

        content = load_template("graph-schema.json")
        data = json.loads(content)
        assert "node_types" in data
        assert "relation_types" in data


class TestBootstrapPrompt:
    def test_render_contains_workspace(self, tmp_path):
        prompt = render_bootstrap_prompt(str(tmp_path))
        assert str(tmp_path) in prompt

    def test_render_contains_graph_schema(self, tmp_path):
        prompt = render_bootstrap_prompt(str(tmp_path))
        assert "node_types" in prompt
        assert "HAS_DOMAIN" in prompt

    def test_render_default_scan_profile(self, tmp_path):
        prompt = render_bootstrap_prompt(str(tmp_path))
        assert "java-spring-microservice" in prompt

    def test_render_custom_scan_profile(self, tmp_path):
        prompt = render_bootstrap_prompt(str(tmp_path), scan_profile="python-service")
        assert "python-service" in prompt

    def test_render_reads_git_commit(self, tmp_path):
        prompt = render_bootstrap_prompt(str(tmp_path))
        # Should contain either a commit hash or "unknown"
        assert "git_commit" in prompt or "unknown" in prompt.lower()


class TestValidator:
    def _make_pack(self, tmp_path, *, missing=None, empty_graph=False):
        """Helper: create a minimal valid knowledge pack."""
        from story_lifecycle.knowledge.scaffold import scaffold_knowledge_dir
        from story_lifecycle.knowledge import paths as kp

        scaffold_knowledge_dir(tmp_path)

        # Create minimal content in scenarios/ and indexes/ to avoid warnings
        (kp.scenarios_dir(tmp_path) / "order").mkdir(exist_ok=True)
        (kp.indexes_dir(tmp_path) / "overview.md").write_text(
            "# Overview\n", encoding="utf-8"
        )

        manifest = {
            "version": 1,
            "product": {"name": "Test", "description": "test"},
            "status": "ready",
            "domains": ["order"],
        }
        if missing != "manifest":
            kp.manifest_path(tmp_path).write_text(yaml.dump(manifest), encoding="utf-8")

        product = {"product": {"name": "Test"}}
        if missing != "product":
            kp.product_path(tmp_path).write_text(yaml.dump(product), encoding="utf-8")

        catalog = "# Search Catalog\n"
        if missing != "search_catalog":
            kp.search_catalog_path(tmp_path).write_text(catalog, encoding="utf-8")

        graph_data = {
            "node_types": [],
            "relation_types": [],
            "nodes": [],
            "edges": [],
        }
        if empty_graph:
            graph_data = {}
        if missing != "graph":
            kp.graph_json_path(tmp_path).write_text(
                json.dumps(graph_data), encoding="utf-8"
            )

    def test_valid_pack_passes(self, tmp_path):
        self._make_pack(tmp_path)
        errors = validate_knowledge_pack(tmp_path)
        assert errors == []

    def test_missing_manifest(self, tmp_path):
        self._make_pack(tmp_path, missing="manifest")
        errors = validate_knowledge_pack(tmp_path)
        assert any("manifest" in e for e in errors)

    def test_missing_product(self, tmp_path):
        self._make_pack(tmp_path, missing="product")
        errors = validate_knowledge_pack(tmp_path)
        assert any("product" in e for e in errors)

    def test_missing_graph(self, tmp_path):
        self._make_pack(tmp_path, missing="graph")
        errors = validate_knowledge_pack(tmp_path)
        assert any("graph" in e for e in errors)

    def test_empty_graph_passes(self, tmp_path):
        """Empty graph (no nodes/edges) is valid — may not have data yet."""
        self._make_pack(tmp_path, empty_graph=True)
        errors = validate_knowledge_pack(tmp_path)
        assert not any("graph" in e for e in errors)

    def test_missing_search_catalog(self, tmp_path):
        self._make_pack(tmp_path, missing="search_catalog")
        errors = validate_knowledge_pack(tmp_path)
        assert any("search-catalog" in e for e in errors)

    def test_invalid_graph_json(self, tmp_path):
        self._make_pack(tmp_path)
        from story_lifecycle.knowledge import paths as kp

        kp.graph_json_path(tmp_path).write_text("not json{{{", encoding="utf-8")
        errors = validate_knowledge_pack(tmp_path)
        assert any("graph" in e.lower() for e in errors)


class TestProjectCLI:
    def test_project_group_registered(self):
        result = CliRunner().invoke(cli, ["project", "--help"])
        assert result.exit_code == 0
        assert "init-knowledge" in result.output

    def test_init_knowledge_help(self):
        result = CliRunner().invoke(cli, ["project", "init-knowledge", "--help"])
        assert result.exit_code == 0

    def test_init_knowledge_dry_run_creates_dirs(self, tmp_path, monkeypatch):
        """--dry-run should create directories without running AI CLI."""
        result = CliRunner().invoke(
            cli, ["project", "init-knowledge", "-w", str(tmp_path), "--dry-run"]
        )
        assert result.exit_code == 0
        assert (tmp_path / ".story" / "knowledge").is_dir()


class TestStale:
    def _write_manifest(self, tmp_path, commit="abc123", ts="2026-01-01T00:00:00"):
        from story_lifecycle.knowledge import paths as kp

        manifest = {
            "version": 1,
            "source": {"commit": commit, "timestamp": ts, "dirty": False},
            "status": "ready",
        }
        kp.manifest_path(tmp_path).parent.mkdir(parents=True, exist_ok=True)
        kp.manifest_path(tmp_path).write_text(yaml.dump(manifest), encoding="utf-8")

    def test_fresh_when_commit_matches(self, tmp_path, monkeypatch):
        self._write_manifest(tmp_path, commit="abc123def456")
        monkeypatch.setattr(
            "story_lifecycle.knowledge.stale._get_git_commit",
            lambda w: "abc123def456",
        )
        result = check_stale(tmp_path)
        assert not result["stale"]

    def test_stale_when_commit_differs(self, tmp_path, monkeypatch):
        self._write_manifest(tmp_path, commit="old_commit")
        monkeypatch.setattr(
            "story_lifecycle.knowledge.stale._get_git_commit",
            lambda w: "new_commit",
        )
        result = check_stale(tmp_path)
        assert result["stale"]
        assert "commit" in result["reason"]

    def test_stale_when_no_manifest(self, tmp_path):
        result = check_stale(tmp_path)
        assert result["stale"]

    def test_stale_when_manifest_status_is_stale(self, tmp_path):
        from story_lifecycle.knowledge import paths as kp

        manifest = {
            "version": 1,
            "source": {"commit": "abc", "timestamp": "2026-01-01"},
            "status": "stale",
        }
        kp.manifest_path(tmp_path).parent.mkdir(parents=True, exist_ok=True)
        kp.manifest_path(tmp_path).write_text(yaml.dump(manifest), encoding="utf-8")
        result = check_stale(tmp_path)
        assert result["stale"]


class TestSearch:
    def _setup_index(self, tmp_path, content):
        from story_lifecycle.knowledge import paths as kp
        from story_lifecycle.knowledge.scaffold import scaffold_knowledge_dir

        scaffold_knowledge_dir(tmp_path)
        idx = kp.indexes_dir(tmp_path) / "api-index.md"
        idx.write_text(content, encoding="utf-8")

    def test_search_finds_keyword(self, tmp_path):
        self._setup_index(
            tmp_path, "# API Index\n\n## /api/withdraw\nwithdraw endpoint\n"
        )
        results = search_knowledge(str(tmp_path), keyword="withdraw")
        assert len(results) > 0
        assert any("withdraw" in r["line"].lower() for r in results)

    def test_search_by_type_filter(self, tmp_path):
        self._setup_index(tmp_path, "# API Index\n\n## /api/withdraw\n")
        results = search_knowledge(str(tmp_path), keyword="withdraw", target_type="api")
        assert all("api" in r["file"] for r in results)

    def test_search_no_results(self, tmp_path):
        self._setup_index(tmp_path, "# API Index\nnothing here\n")
        results = search_knowledge(str(tmp_path), keyword="nonexistent_xyz")
        assert results == []

    def test_search_limit(self, tmp_path):
        content = "# API Index\n" + "\n".join(
            f"## /api/withdraw/{i}\nwithdraw endpoint {i}\n" for i in range(50)
        )
        self._setup_index(tmp_path, content)
        results = search_knowledge(str(tmp_path), keyword="withdraw", limit=5)
        assert len(results) <= 5


class TestCreateStoryKnowledgeHint:
    """创建 story 时，如果缺少知识包，应给出提示。"""

    def test_create_without_knowledge_shows_hint(self, tmp_path, monkeypatch):
        monkeypatch.setattr("story_lifecycle.cli.main.init_db", lambda: None)
        monkeypatch.setattr("story_lifecycle.cli.main.is_configured", lambda: True)
        monkeypatch.setattr("story_lifecycle.cli.main.load_config_to_env", lambda: None)
        monkeypatch.setattr(
            "story_lifecycle.orchestrator.service.create_and_start_story",
            lambda **kw: kw["story_key"],
        )
        monkeypatch.setattr(
            "story_lifecycle.orchestrator.graph.start_story_async",
            lambda key: None,
        )

        result = CliRunner().invoke(
            cli, ["create", "TEST-001", "-t", "test", "-w", str(tmp_path), "--no-start"]
        )
        assert "init-knowledge" in result.output or "知识包" in result.output
