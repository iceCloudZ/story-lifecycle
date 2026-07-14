"""Tests for _build_cli_prompt — the prompt handed to headless agents (kimi/claude).

Root-cause guard (real-run 2026-07-06): in code-writing stages kimi-code self-verified by
running ``mvn compile`` + ``tsc --noEmit`` on large Java/Vue repos -> blocked many minutes
-> never reached the done handshake -> stage failed (build/first-verify: no done; design +
verify-round1 which didn't compile: done written fine). The prompt must explicitly forbid
heavy build/compile/test commands so the agent writes code + done instead of blocking.
"""

import json

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


class TestDesignDimensions:
    """design 阶段注入「维度 checklist + 高价值维度 playbook」。
    brainstorming 与 checklist 共存(BUG #14:不再禁止 brainstorming)。"""

    def test_design_stage_has_dimension_checklist(self, tmp_path):
        p = _build("design", tmp_path)
        assert "设计维度" in p or "维度 checklist" in p
        # 13 维度关键词抽检
        assert "数据模型" in p and "安全" in p and "降级" in p and "接口契约" in p

    def test_design_stage_allows_brainstorming(self, tmp_path):
        """BUG #14: brainstorming 不再被禁止,与 checklist 共存。"""
        p = _build("design", tmp_path)
        assert "brainstorming" in p
        # 不应出现禁止性措辞
        assert "不要调用 brainstorming" not in p
        assert "禁止" not in p

    def test_design_stage_injects_playbook_guides(self, tmp_path):
        """BUG #15: playbook 改触发式引导(不全量塞框架)。"""
        pb_dir = tmp_path / ".story" / "knowledge" / "playbooks"
        pb_dir.mkdir(parents=True)
        (pb_dir / "security-parameter-trust.md").write_text(
            "## 框架\n安全内容MARKER\n## 怎么用\nagent 注入",
            encoding="utf-8",
        )
        (pb_dir / "degradation-fallback.md").write_text(
            "## 框架\n降级内容MARKER\n## 怎么用\nagent 注入",
            encoding="utf-8",
        )
        p = _build("design", tmp_path)
        # 触发式引导:注入引导行(两个 playbook 都引导)
        assert "安全" in p and "先读" in p
        assert "降级兼容" in p and "先读" in p
        # 全量框架内容不应出现在 prompt 里(只在文件里,claude 按需自查)
        assert "安全内容MARKER" not in p
        assert "降级内容MARKER" not in p

    def test_design_stage_no_playbook_still_has_checklist(self, tmp_path):
        # workspace 无 playbook 时,维度 checklist 仍在(不阻塞)
        p = _build("design", tmp_path)
        assert "数据模型" in p

    def test_non_design_stage_no_dimensions(self, tmp_path):
        p = _build("build", tmp_path)
        assert "设计维度 checklist" not in p

    def test_design_stage_instructs_mcp_clarify_protocol(self, tmp_path):
        """design prompt 指示遇关键歧义调 mcp__lifecycle__clarify 工具(外接 MCP HITL)。

        方向变更(2026-07-07):从侧文件协议改为外接 MCP clarify 工具——实测 claude
        真的调用它并用人答继续(context 保留)。见 memory story-lifecycle-design-hitl。
        """
        p = _build("design", tmp_path)
        assert "不要提澄清问题" not in p  # 旧禁令移除
        assert "mcp__lifecycle__clarify" in p  # 指示调 MCP clarify 工具
        assert "clarify_request.json" not in p  # 侧文件协议已移除
        # 触发条件:遇关键歧义才问(非无脑问)
        assert "歧义" in p or "岔路" in p
