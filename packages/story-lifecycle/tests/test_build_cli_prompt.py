"""Tests for _build_cli_prompt — the prompt handed to headless agents (kimi/claude).

Root-cause guard (real-run 2026-07-06): in code-writing stages kimi-code self-verified by
running ``mvn compile`` + ``tsc --noEmit`` on large Java/Vue repos -> blocked many minutes
-> never reached the done handshake -> stage failed (build/first-verify: no done; design +
verify-round1 which didn't compile: done written fine). The prompt must explicitly forbid
heavy build/compile/test commands so the agent writes code + done instead of blocking.
"""

from story_lifecycle.orchestrator.engine.planner import _build_cli_prompt


def _build(stage, tmp_path, **kw):
    return _build_cli_prompt(
        story_key="S-1",
        title="t",
        stage=stage,
        focus="impl the feature",
        done_file=f".story/done/S-1/{stage}.json",
        profile_stages={},
        prd_path="",
        project_section="",
        workspace=str(tmp_path),
        transcript_section="",
        **kw,
    )


class TestNoHeavyBuildCommands:
    def test_build_stage_forbids_mvn_tsc(self, tmp_path):
        p = _build("build", tmp_path)
        # must name the offending tools (kimi ran exactly mvn + tsc) + forbid running them
        assert "mvn" in p
        assert "tsc" in p
        assert ("不要运行" in p) or ("禁止运行" in p) or ("不要执行" in p)

    def test_verify_stage_also_forbids(self, tmp_path):
        p = _build("verify", tmp_path)
        assert "mvn" in p and "tsc" in p

    def test_done_handshake_still_present(self, tmp_path):
        # the constraint must not displace the completion protocol
        p = _build("build", tmp_path)
        assert "完成协议" in p
        assert ".story/done/S-1/build.json" in p

    def test_design_stage_also_gets_constraint(self, tmp_path):
        # unconditional guard (harmless for design — it doesn't compile anyway)
        p = _build("design", tmp_path)
        assert "mvn" in p
