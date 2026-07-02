"""ISS-009 9a contract test: KnowledgeContextProvider surfaces playbook/scenario/failure
knowledge via the `knowledge` contract package (KnowledgeIndex.retrieve), turning the
④ knowledge layer into a runtime contract. Verifies the wiring is live (not aspirational)
when the knowledge package is present, and degrades gracefully when absent."""

import json
import sys
from pathlib import Path

import pytest


def _ensure_knowledge_importable():
    """Make the monorepo knowledge package importable (packages/knowledge/src).

    story-lifecycle does not declare story-knowledge as a runtime dep yet, so in
    the standalone install path `from knowledge import KnowledgeIndex` ImportErrors
    and the provider degrades gracefully. In the monorepo test env we put it on
    sys.path to exercise the real integration.
    """
    ksrc = Path(__file__).resolve().parents[2] / "knowledge" / "src"
    if ksrc.is_dir() and str(ksrc) not in sys.path:
        sys.path.insert(0, str(ksrc))
    import knowledge  # noqa: F401
    return knowledge


def test_get_context_surfaces_knowledge_index_playbook(monkeypatch, tmp_path):
    """A playbook linked to the story is surfaced via KnowledgeIndex.retrieve()."""
    try:
        _ensure_knowledge_importable()
    except ImportError:
        pytest.skip("knowledge package not available in this monorepo checkout")

    from story_lifecycle.knowledge.context_providers import knowledge_provider as kp

    # knowledge dir with one playbook linked to the story
    kdir = tmp_path / "knowledge"
    kdir.mkdir()
    (kdir / "INDEX.json").write_text(
        json.dumps(
            {
                "entries": [
                    {
                        "type": "playbook",
                        "id": "pb-amount-validate",
                        "title": "Always validate amounts are positive",
                        "source": "miner",
                        "linked_story": "TEST-001",
                        "tags": ["amount", "validation"],
                        "must_read": ["confirm amount > 0 at boundary"],
                        "top_files": [],
                        "common_commands": [],
                        "common_failures": [],
                        "linked_scenarios": [],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(kp, "_KNOWLEDGE_ROOT", kdir)
    # No raw miner artifacts under base_path -> output relies on bootstrap + knowledge
    provider = kp.KnowledgeContextProvider(
        config={"base_path": str(tmp_path / "no-artifacts")}
    )
    monkeypatch.setattr(provider, "_task_type_for", lambda key: "fund-flow")

    out = provider.get_context("TEST-001", str(tmp_path), "design")

    assert out is not None
    # The playbook title must appear — proving KnowledgeIndex.retrieve() wired through
    assert "Always validate amounts are positive" in out
    assert "必读" in out  # must_read rendered


def test_get_context_degrades_gracefully_without_knowledge_pkg(monkeypatch, tmp_path):
    """If `knowledge` can't be imported, get_context still returns the rest (no crash)."""
    import builtins

    from story_lifecycle.knowledge.context_providers import knowledge_provider as kp

    real_import = builtins.__import__

    def _block_knowledge(name, *args, **kwargs):
        if name == "knowledge":
            raise ImportError("blocked for test")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _block_knowledge)
    provider = kp.KnowledgeContextProvider(
        config={"base_path": str(tmp_path / "no-artifacts")}
    )
    monkeypatch.setattr(provider, "_task_type_for", lambda key: "fund-flow")

    out = provider.get_context("TEST-001", str(tmp_path), "design")
    # No crash; returns the bootstrap/structure output (knowledge section silently absent)
    assert out is not None
    assert "飞轮知识上下文" in out
