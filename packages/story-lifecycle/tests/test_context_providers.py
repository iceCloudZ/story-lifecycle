"""Tests for the context_providers loader + protocol."""

import pytest

import story_lifecycle.knowledge.context_providers as cp
from story_lifecycle.knowledge.context_providers import get_transcript_context
from story_lifecycle.knowledge.context_providers.base import BaseStoryContextProvider


class _Fake:
    def __init__(self, config=None):
        self.config = config or {}

    def get_context(self, story_key, workspace, stage):
        return f"DEFAULT_CTX for {story_key}/{stage}"


class TestContextProviderLoader:
    def test_no_config_and_no_default_returns_none(self, monkeypatch):
        monkeypatch.setattr(cp, "get_config", lambda: {})
        monkeypatch.setattr(cp, "_default_provider_cfg", lambda: None)
        assert get_transcript_context("k", "ws", "design") is None

    def test_no_config_uses_default_miner_provider(self, monkeypatch):
        """When user has no context_provider config but miner is available."""
        monkeypatch.setattr(cp, "get_config", lambda: {})
        monkeypatch.setattr(cp, "_default_provider_cfg", lambda: {
            "module": "miner.story_context_provider",
            "class": "TranscriptStoryContextProvider",
            "db_path": ":memory:",
        })
        monkeypatch.setattr(cp, "_load_provider", lambda cfg: _Fake(cfg))
        assert get_transcript_context("tapd-1065518", "D:/hc-all", "design") == "DEFAULT_CTX for tapd-1065518/design"

    def test_no_context_provider_key_returns_none(self, monkeypatch):
        monkeypatch.setattr(cp, "get_config", lambda: {"api_key": "x"})
        monkeypatch.setattr(cp, "_default_provider_cfg", lambda: None)
        assert get_transcript_context("k", "ws", "design") is None

    def test_loads_provider_dynamically(self, monkeypatch):
        monkeypatch.setattr(cp, "get_config", lambda: {
            "context_provider": {
                "module": "tests._fake_context_provider",
                "class": "FakeProvider",
            }
        })
        result = get_transcript_context("tapd-1065518", "D:/hc-all", "design")
        assert result == "FAKE_CONTEXT for tapd-1065518/design"

    def test_provider_error_returns_none(self, monkeypatch):
        monkeypatch.setattr(cp, "get_config", lambda: {
            "context_provider": {
                "module": "tests._fake_context_provider",
                "class": "ErrorProvider",
            }
        })
        assert get_transcript_context("k", "ws", "design") is None

    def test_missing_class_returns_none(self, monkeypatch):
        monkeypatch.setattr(cp, "get_config", lambda: {
            "context_provider": {
                "module": "tests._fake_context_provider",
                "class": "NoSuchClass",
            }
        })
        assert get_transcript_context("k", "ws", "design") is None

    def test_path_config_enables_external_package(self, monkeypatch, tmp_path):
        """Provider in a package NOT on sys.path — loader adds config 'path'."""
        pkg = tmp_path / "extpkg"
        pkg.mkdir()
        (pkg / "__init__.py").write_text("", encoding="utf-8")
        (pkg / "mod.py").write_text(
            "class ExtProvider:\n"
            "    def __init__(self, config=None):\n"
            "        self.config = config or {}\n"
            "    def get_context(self, story_key, workspace, stage):\n"
            "        return 'EXT_OK'\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(cp, "get_config", lambda: {
            "context_provider": {
                "module": "extpkg.mod",
                "class": "ExtProvider",
                "path": str(tmp_path),
            }
        })
        assert "extpkg.mod:ExtProvider" not in cp._PROVIDERS
        assert get_transcript_context("k", "ws", "design") == "EXT_OK"


class TestBaseStoryContextProvider:
    def test_abstract_cannot_instantiate(self):
        with pytest.raises(TypeError):
            BaseStoryContextProvider()
