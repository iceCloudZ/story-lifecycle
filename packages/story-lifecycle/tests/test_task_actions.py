"""Tests for task_actions module (DESIGN-task-actions-and-grill-me.md)。

预制动作库 + prompt 组装。LLM 从库里选动作,Python 按 order 排序后拼成任务清单。
执行约束由 task_actions 内容决定(选了 run_tests 就允许轻量测试)。
"""

from story_lifecycle.orchestrator.engine.task_actions import (
    TASK_ACTIONS,
    _build_task_list,
    _build_exec_constraint,
    get_expected_outputs,
    get_default_task_actions,
    get_action_catalog_for_prompt,
)


class TestTaskActionsLibrary:
    """动作库完整性。"""

    def test_all_actions_have_required_fields(self):
        """每个动作有 desc/instruction/order/mode/expected_output_key。"""
        for key, action in TASK_ACTIONS.items():
            assert "desc" in action, f"{key} missing desc"
            assert "instruction" in action, f"{key} missing instruction"
            assert "order" in action, f"{key} missing order"
            assert "mode" in action, f"{key} missing mode"
            assert "expected_output_key" in action, f"{key} missing expected_output_key"

    def test_orders_are_unique(self):
        """order 值不重复(排序不会撞)。"""
        orders = [a["order"] for a in TASK_ACTIONS.values()]
        assert len(orders) == len(set(orders))

    def test_six_actions(self):
        """当前 6 个动作(write_design_doc/write_code/run_tests/accept_review/write_test_report/write_delivery_doc)。"""
        expected = {"write_design_doc", "write_code", "run_tests",
                    "accept_review", "write_test_report", "write_delivery_doc"}
        assert set(TASK_ACTIONS.keys()) == expected


class TestBuildTaskList:
    """任务清单组装(R1:LLM 只管选,Python 按 order 排)。"""

    def test_sorted_by_order(self):
        """LLM 返回乱序 → 按 order 排序。"""
        tl = _build_task_list(["write_code", "write_design_doc", "run_tests"])
        # design(order=10) 在 code(order=20) 在 tests(order=30) 前
        idx_design = tl.index("调研现有代码")
        idx_code = tl.index("实现代码改动")
        idx_tests = tl.index("运行测试")
        assert idx_design < idx_code < idx_tests

    def test_empty_list_returns_empty(self):
        assert _build_task_list([]) == ""

    def test_invalid_keys_filtered(self):
        """无效 key 被过滤,不崩。"""
        tl = _build_task_list(["write_code", "nonexistent_action"])
        assert "实现代码改动" in tl
        assert len(tl.split("\n")) > 0

    def test_has_header(self):
        tl = _build_task_list(["write_code"])
        assert "本阶段任务清单" in tl
        assert "按以下顺序完成" in tl


class TestBuildExecConstraint:
    """执行约束由 task_actions 内容决定。"""

    def test_with_run_tests_allows_lightweight(self):
        c = _build_exec_constraint(["write_code", "run_tests"])
        assert "轻量自检" in c or "pytest" in c
        assert "mvn" in c  # 重构建仍禁

    def test_without_run_tests_forbids(self):
        c = _build_exec_constraint(["write_design_doc"])
        assert "不要运行" in c or "不需要跑测试" in c

    def test_empty_actions_forbids(self):
        c = _build_exec_constraint([])
        assert "不要运行" in c or "不需要跑测试" in c


class TestExpectedOutputs:
    """Q3:task_actions 联动 done.json 期望字段。"""

    def test_design_doc_yields_spec_path(self):
        assert "spec_path" in get_expected_outputs(["write_design_doc"])

    def test_test_report_yields_test_report_path(self):
        assert "test_report_path" in get_expected_outputs(["write_test_report"])

    def test_multiple_actions_aggregate(self):
        outputs = get_expected_outputs(["write_design_doc", "write_code", "write_test_report"])
        assert "spec_path" in outputs
        assert "files_changed" in outputs
        assert "test_report_path" in outputs

    def test_run_tests_no_expected_output(self):
        """run_tests 没有对应 output key(它不产出文件,只跑测试)。"""
        assert get_expected_outputs(["run_tests"]) == []

    def test_no_duplicates(self):
        outputs = get_expected_outputs(["write_code", "write_code"])
        assert outputs.count("files_changed") == 1


class TestGetDefaultTaskActions:
    """fallback:LLM 不可用时按 stage 名给默认动作。"""

    def test_design_default(self):
        assert get_default_task_actions("design") == ["write_design_doc"]

    def test_build_default(self):
        assert get_default_task_actions("build") == ["write_code"]

    def test_verify_default(self):
        result = get_default_task_actions("verify")
        assert "run_tests" in result
        assert "accept_review" in result
        assert "write_test_report" in result

    def test_single_stage_all_actions(self):
        """单 stage profile → 全干。"""
        result = get_default_task_actions("verify", is_single_stage=True)
        assert "write_design_doc" in result
        assert "write_code" in result
        assert "run_tests" in result

    def test_unknown_stage_fallback(self):
        """未知 stage → 默认 write_code。"""
        assert get_default_task_actions("unknown_stage") == ["write_code"]


class TestActionCatalogForPrompt:
    """system prompt 用的动作目录。"""

    def test_lists_all_actions(self):
        cat = get_action_catalog_for_prompt()
        for key in TASK_ACTIONS:
            assert key in cat

    def test_has_recommendation(self):
        """Q2:给推荐模式(常识,非硬规则)。"""
        cat = get_action_catalog_for_prompt()
        assert "推荐模式" in cat or "单阶段全干" in cat
