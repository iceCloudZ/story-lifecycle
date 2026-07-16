"""Tests for _build_cli_prompt — the prompt handed to headless agents (kimi/claude).

Root-cause guard (real-run 2026-07-06): in code-writing stages kimi-code self-verified by
running ``mvn compile`` + ``tsc --noEmit`` on large Java/Vue repos -> blocked many minutes
-> never reached the done handshake -> stage failed. The prompt must explicitly forbid
heavy build/compile/test commands so the agent writes code + done instead of blocking.

REFACTOR task_actions: 执行约束现在由 task_actions 内容决定(选了 run_tests 就允许
轻量测试,没选就禁)。不再按 stage 名硬编码。
"""

from story_lifecycle.orchestrator.engine.planner import _build_cli_prompt


def _build(stage, tmp_path, **kw):
    defaults = {
        "story_key": "S-1",
        "title": "t",
        "stage": stage,
        "focus": "impl the feature",
        "done_file": f".story/done/S-1/{stage}.json",
        "profile_stages": {},
        "prd_path": "",
        "project_section": "",
        "workspace": str(tmp_path),
        "transcript_section": "",
    }
    defaults.update(kw)  # kw 覆盖默认(允许定制 profile_stages 等)
    return _build_cli_prompt(**defaults)


class TestExecConstraint:
    """执行约束由 task_actions 决定(替 stage 名硬编码)。"""

    def test_no_task_actions_forbids_tests(self, tmp_path):
        """无 task_actions → 禁测试(默认只写代码)。"""
        p = _build("build", tmp_path)
        assert "mvn" in p
        assert "不要运行" in p or "不要跑" in p

    def test_with_run_tests_allows_lightweight(self, tmp_path):
        """选了 run_tests → 允许轻量自检。"""
        p = _build("verify", tmp_path, task_actions=["write_code", "run_tests"])
        assert "pytest" in p or "轻量自检" in p
        assert "mvn" in p  # 重构建仍禁

    def test_without_run_tests_forbids_all_tests(self, tmp_path):
        """没选 run_tests → 禁所有测试。"""
        p = _build("design", tmp_path, task_actions=["write_design_doc"])
        assert "不要运行" in p or "不需要跑测试" in p

    def test_done_handshake_still_present(self, tmp_path):
        p = _build("build", tmp_path)
        assert "完成协议" in p
        assert ".story/done/S-1/build.json" in p


class TestTaskListSection:
    """task_actions → 任务清单段(按 order 排序)。"""

    def test_task_list_in_prompt(self, tmp_path):
        p = _build("verify", tmp_path, task_actions=["write_code", "run_tests"])
        assert "本阶段任务清单" in p
        assert "按以下顺序完成" in p

    def test_task_list_sorted_by_order(self, tmp_path):
        """LLM 返回乱序,Python 按 order 排(write_design_doc 在 write_code 前)。"""
        p = _build("verify", tmp_path,
                   task_actions=["write_code", "write_design_doc"])
        idx_design = p.index("调研现有代码")
        idx_code = p.index("实现代码改动")
        assert idx_design < idx_code  # design(order=10) 在 code(order=20) 前

    def test_empty_task_actions_no_task_list(self, tmp_path):
        p = _build("build", tmp_path, task_actions=[])
        assert "本阶段任务清单" not in p


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


class TestSinglePassVerifyPrompt:
    """single-pass verify prompt 修复回归(真实全链路 tapd-...1066752 暴露)。

    三层根因:
    1. LLM 规划路径缺 single-pass 保底 → task_actions 漏 run_tests → 任务清单段丢
       + 禁测试约束(与 single-pass 全干语义矛盾)。
    2. 完成协议写死 4 字段 → CLI 不知道要交 test_report_path(done 校验无源失败)。
    3. grill 段 gate 对 single-pass verify 关闭 → PRD 岔路无澄清协议兜底。
    """

    def test_single_pass_verify_has_full_guidance(self, tmp_path):
        """single-pass verify(profile 单 stage + task_actions 含 run_tests)拿到全引导:
        任务清单 + 允许自检 + 动态完成协议(含 test_report_path) + grill 澄清协议。"""
        p = _build(
            "verify",
            tmp_path,
            task_actions=[
                "write_design_doc",
                "write_code",
                "run_tests",
                "accept_review",
                "write_test_report",
            ],
            grill=True,
            is_single_stage=True,
            profile_stages={"verify": {"cli": "claude"}},
        )
        # 任务清单段在
        assert "本阶段任务清单" in p
        # 允许轻量自检(不是禁测试)
        assert "轻量自检" in p or "pytest" in p
        # 动态完成协议含 test_report_path(选了 write_test_report)
        assert "test_report_path" in p
        # grill 澄清协议段在
        assert "grill-me" in p or "澄清协议" in p

    def test_single_pass_grill_even_when_stage_is_verify(self, tmp_path):
        """grill 段对 single-pass verify 开放(原本 gate `stage != "design"` 把它关了)。

        多阶段 verify 仍由 grill 标志位控制(不会因本改动泄漏)。
        """
        # single-pass verify + grill=True → 有 grill 段
        p_single = _build(
            "verify",
            tmp_path,
            grill=True,
            is_single_stage=True,
            profile_stages={"verify": {"cli": "claude"}},
        )
        assert "grill-me" in p_single or "澄清协议" in p_single
        # 多阶段 verify + grill=False → 无 grill 段(防过度放开)
        p_multi = _build(
            "verify",
            tmp_path,
            grill=False,
            is_single_stage=False,
            profile_stages={"design": {"cli": "claude"}, "verify": {"cli": "claude"}},
        )
        assert "grill-me" not in p_multi
        assert "澄清协议" not in p_multi

    def test_done_protocol_dynamic_fields_by_task_actions(self, tmp_path):
        """完成协议字段随 task_actions 动态(改动 2):选 write_design_doc → spec_path;
        选 write_test_report → test_report_path;都不选 → 只基础 4 字段。"""
        # 选了 write_design_doc + write_test_report
        p_full = _build(
            "verify",
            tmp_path,
            task_actions=["write_design_doc", "write_test_report"],
        )
        assert "spec_path" in p_full
        assert "test_report_path" in p_full
        # 都不选 → 只基础字段(spec_path/test_report_path 不出现)
        p_bare = _build("verify", tmp_path, task_actions=["write_code"])
        assert "spec_path" not in p_bare
        assert "test_report_path" not in p_bare
        # 基础字段始终在
        assert "完成协议" in p_bare
        assert "files_changed" in p_bare


class TestSinglePassPlanningFallback:
    """LLM 规划路径的 single-pass 保底(改动 1):helper 函数级别锁定。

    LLM 漏选 run_tests / 没给 grill 时,helper 补上——对齐 fallback 路径的语义。
    """

    def test_ensure_single_pass_actions_adds_run_tests(self):
        from story_lifecycle.orchestrator.engine.planner import (
            _ensure_single_pass_actions,
        )

        # single-pass 且漏 run_tests → 补上
        out = _ensure_single_pass_actions(["write_code"], is_single=True)
        assert "run_tests" in out
        # 已含 run_tests → 不重复
        out2 = _ensure_single_pass_actions(
            ["write_code", "run_tests"], is_single=True
        )
        assert out2.count("run_tests") == 1
        # 多阶段 → 直通,不补
        out3 = _ensure_single_pass_actions(["write_code"], is_single=False)
        assert "run_tests" not in out3

    def test_resolve_single_pass_grill_defaults_true(self):
        from story_lifecycle.orchestrator.engine.planner import (
            _resolve_single_pass_grill,
        )

        # single-pass + None → 保底 True(LLM 没想清楚时兜底)
        assert _resolve_single_pass_grill(None, is_single=True) is True
        # single-pass + 显式 False → 尊重(False)
        assert _resolve_single_pass_grill(False, is_single=True) is False
        # 多阶段 + None → False(不兜底)
        assert _resolve_single_pass_grill(None, is_single=False) is False
