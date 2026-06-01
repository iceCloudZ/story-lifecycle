from pathlib import Path
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
