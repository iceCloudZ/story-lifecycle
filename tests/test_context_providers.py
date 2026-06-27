"""Tests for the context_providers loader + protocol."""

import pytest

import story_lifecycle.context_providers as cp
from story_lifecycle.context_providers import get_transcript_context
from story_lifecycle.context_providers.base import BaseStoryContextProvider


class TestContextProviderLoader:
    def test_no_config_returns_none(self, monkeypatch):
        monkeypatch.setattr(cp, "get_config", lambda: {})
        assert get_transcript_context("k", "ws", "design") is None

    def test_no_context_provider_key_returns_none(self, monkeypatch):
        monkeypatch.setattr(cp, "get_config", lambda: {"api_key": "x"})
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


class TestBaseStoryContextProvider:
    def test_abstract_cannot_instantiate(self):
        with pytest.raises(TypeError):
            BaseStoryContextProvider()
