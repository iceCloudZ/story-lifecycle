"""设计 13 Step 1：抽象类契约测试（abc.py）。

覆盖：
- ABC 不能直接实例化
- 子类缺抽象方法 → TypeError
- DecisionHandler 三个 handle 方法都在接口里
- PromptBuilder.build 签名包含 story_key/stage
"""

import inspect
import pytest

from story_lifecycle.orchestrator.abc import (
    DecisionHandler,
    PromptBuilder,
    StageExecutor,
)


class TestAbcContracts:
    def test_stage_executor_cannot_instantiate_directly(self):
        """ABC 不能实例化"""
        with pytest.raises(TypeError):
            StageExecutor()

    def test_stage_executor_subclass_must_implement_all(self):
        """缺一个方法就 TypeError"""
        class Incomplete(StageExecutor):
            pass

        with pytest.raises(TypeError):
            Incomplete()

    def test_stage_executor_subclass_with_all_methods_ok(self):
        """三个抽象方法都实现 → 可实例化；maybe_spawn 有默认实现"""
        class Complete(StageExecutor):
            def get_pty(self, story_key, stage):
                return None

            def spawn(self, story_key, stage, action):
                return "sid"

            def is_artifacts_ready(self, story_key, stage):
                return False

        ex = Complete()
        assert ex.get_pty("k", "design") is None
        assert ex.spawn("k", "design", {}) == "sid"
        assert ex.is_artifacts_ready("k", "design") is False
        # 默认 maybe_spawn 是 no-op（半自动行为）
        ex.maybe_spawn("k", "design", {})
        assert ex.get_pty("k", "design") is None

    def test_decision_handler_cannot_instantiate_directly(self):
        with pytest.raises(TypeError):
            DecisionHandler()

    def test_decision_handler_three_branches_exist(self):
        """三个 handle 方法都在接口里"""
        assert hasattr(DecisionHandler, "handle_approve")
        assert hasattr(DecisionHandler, "handle_reject")
        assert hasattr(DecisionHandler, "handle_escalate")

    def test_decision_handler_subclass_must_implement_all(self):
        class Incomplete(DecisionHandler):
            def handle_approve(self, *a, **k):
                return False

        with pytest.raises(TypeError):
            Incomplete()

    def test_prompt_builder_cannot_instantiate_directly(self):
        with pytest.raises(TypeError):
            PromptBuilder()

    def test_prompt_builder_build_signature(self):
        """build 方法签名正确"""
        sig = inspect.signature(PromptBuilder.build)
        params = list(sig.parameters)
        for name in ("story_key", "stage", "workspace", "ctx", "action"):
            assert name in params, f"build 缺参数 {name}: {params}"

    def test_prompt_builder_subclass_ok(self):
        class Simple(PromptBuilder):
            def build(self, story_key, stage, workspace, ctx, action):
                return f"{stage}:{story_key}"

        assert Simple().build("k", "design", "/ws", {}, {}) == "design:k"
