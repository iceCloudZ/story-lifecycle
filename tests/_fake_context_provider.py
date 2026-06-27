"""Fake context provider for testing the importlib-based loader.

Importable as ``tests._fake_context_provider`` — lets ``test_context_providers``
verify dynamic loading / error swallowing without depending on the real miner.
"""


class FakeProvider:
    """Returns a deterministic string including story_key and stage."""

    def __init__(self, config=None):
        self.config = config or {}

    def get_context(self, story_key, workspace, stage):
        return f"FAKE_CONTEXT for {story_key}/{stage}"


class ErrorProvider:
    """Always raises — verifies the loader swallows provider errors."""

    def __init__(self, config=None):
        self.config = config or {}

    def get_context(self, story_key, workspace, stage):
        raise RuntimeError("provider boom")
