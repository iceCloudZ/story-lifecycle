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
    """design 阶段注入「维度 checklist + 禁 brainstorming + 高价值维度 playbook」,
    替代 brainstorming 自由探索(在 hc-all 重环境发散/context rot,见 runbook §7.4)。"""

    def test_design_stage_has_dimension_checklist(self, tmp_path):
        p = _build("design", tmp_path)
        assert "设计维度" in p or "维度 checklist" in p
        # 13 维度关键词抽检
        assert "数据模型" in p and "安全" in p and "降级" in p and "接口契约" in p

    def test_design_stage_forbids_brainstorming(self, tmp_path):
        p = _build("design", tmp_path)
        assert "brainstorming" in p
        assert ("不要调用" in p) or ("禁止" in p) or ("不要调" in p)

    def test_design_stage_injects_security_playbook(self, tmp_path):
        # 造假 workspace + security playbook,验注入逻辑(不依赖真实 hc-all)
        pb_dir = tmp_path / ".story" / "knowledge" / "playbooks"
        pb_dir.mkdir(parents=True)
        (pb_dir / "security-parameter-trust.md").write_text(
            "## 框架\nCORE 参数:金额/产品编码/身份/用户标识\n## 怎么用\nagent 注入",
            encoding="utf-8",
        )
        p = _build("design", tmp_path)
        assert "CORE" in p  # playbook 片段(怎么用 之前)注入了

    def test_design_stage_no_playbook_still_has_checklist(self, tmp_path):
        # workspace 无 playbook 时,维度 checklist 仍在(不阻塞)
        p = _build("design", tmp_path)
        assert "数据模型" in p

    def test_non_design_stage_no_dimensions(self, tmp_path):
        p = _build("build", tmp_path)
        assert "设计维度 checklist" not in p

    def test_design_stage_instructs_clarify_protocol(self, tmp_path):
        """design prompt 指示侧文件 clarify 协议(替代『不要提澄清问题』)。

        runbook 块1 + 架构偏差:claude -p 无 AskUserQuestion → 改指示 claude 遇关键
        歧义写 clarify_request.json 后停(不写 design.json),由编排层接住 HITL。
        clarify 路径取自 done file 同目录(= poll loop 查的位置)。
        """
        p = _build("design", tmp_path)
        assert "不要提澄清问题" not in p  # 旧禁令移除
        assert "clarify_request.json" in p  # 新协议文件名
        # 路径取自 done file 同目录(.story/done/S-1/),与 poll loop 一致
        assert ".story/done/S-1/clarify_request.json" in p
        # 触发条件:遇关键歧义才问(非无脑问)
        assert "歧义" in p or "岔路" in p

    def test_design_stage_injects_clarify_history_when_present(self, tmp_path):
        """回注后重启 claude:prompt 注入已澄清 Q&A 历史(基于前答继续,动态澄清)。"""
        from story_lifecycle.orchestrator.engine.clarify import clarify_history_rel

        hist_path = tmp_path / clarify_history_rel(".story/done/S-1/design.json")
        hist_path.parent.mkdir(parents=True, exist_ok=True)
        hist_path.write_text(
            json.dumps([{"question": "配置存哪?", "answer": "hc_user"}]),
            encoding="utf-8",
        )

        p = _build("design", tmp_path)
        assert "已澄清的历史问答" in p
        assert "配置存哪?" in p
        assert "hc_user" in p

    def test_design_stage_no_history_section_when_absent(self, tmp_path):
        """无 clarify_history → 不注入历史段(回归守护,首次 design 无历史)。"""
        p = _build("design", tmp_path)
        assert "已澄清的历史问答" not in p
