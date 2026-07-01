"""Tests for _render_prompt transcript-context injection (dry-run / template path)."""

import story_lifecycle.context_providers as cp


class TestRenderPromptTranscript:
    def test_transcript_context_injected(self, isolated_story_home, monkeypatch):
        from story_lifecycle.orchestrator.engine.prompt_renderer import _render_prompt

        monkeypatch.setattr(
            cp,
            "get_transcript_context",
            lambda *a, **k: "### 历史上下文\n- 既往调研 hc-user",
        )
        state = {
            "story_key": "TEST-TCTX",
            "title": "T",
            "workspace": str(isolated_story_home),
            "context": {},
        }
        prompt, meta = _render_prompt("design", state)
        assert "既往调研 hc-user" in prompt
        assert meta["transcript_context"] == "### 历史上下文\n- 既往调研 hc-user"

    def test_transcript_context_none_omitted(self, isolated_story_home, monkeypatch):
        from story_lifecycle.orchestrator.engine.prompt_renderer import _render_prompt

        monkeypatch.setattr(cp, "get_transcript_context", lambda *a, **k: None)
        state = {
            "story_key": "TEST-TCTX2",
            "title": "T",
            "workspace": str(isolated_story_home),
            "context": {},
        }
        prompt, meta = _render_prompt("design", state)
        assert "{transcript_context}" not in prompt
        assert meta["transcript_context"] == ""
