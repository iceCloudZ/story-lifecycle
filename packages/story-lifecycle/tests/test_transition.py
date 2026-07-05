"""Tests for transition Decider(层2 stage 转移)。

``decide_transition`` 纯 Decider:
``(gate_decision, failure_mode, history_facts) -> {action, reason, ...}``。
替 planner.py:769-797 硬编码 ``actions.insert()`` —— 按历史 + 失败模式智能选转移。
零副作用,规则驱动。守 §2.2 #1。

action 取值:``proceed`` / ``retry`` / ``skip`` / ``swap_approach`` / ``insert_rescue_stage`` / ``escalate``。
"""

from story_lifecycle.orchestrator.engine.transition import decide_transition


class TestDecideTransition:
    def test_gate_passed_proceeds(self):
        """gate 过 → proceed 到下一 stage。"""
        r = decide_transition(
            gate_decision={"pass": True},
            failure_mode=None,
            history_facts={},
        )
        assert r["action"] == "proceed"

    def test_gate_fail_history_swap_succeeded_returns_swap_approach(self):
        """gate fail + 历史"同类失败换 adapter 成功" → swap_approach(非硬编码 insert)。"""
        r = decide_transition(
            gate_decision={"pass": False, "rework_point": "tests"},
            failure_mode="tests",
            history_facts={"same_failure_swap_succeeded": True, "failure_count_on_stage": 1},
        )
        assert r["action"] == "swap_approach"
        assert isinstance(r["reason"], str) and r["reason"]

    def test_missing_dependency_inserts_rescue_stage(self):
        """失败模式是缺依赖 → insert_rescue_stage(带 rescue_stage 名)。"""
        r = decide_transition(
            gate_decision={"pass": False},
            failure_mode="missing_dependency",
            history_facts={"missing_dep": "python-dotenv"},
        )
        assert r["action"] == "insert_rescue_stage"
        assert r.get("rescue_stage")

    def test_repeated_failure_beyond_max_escalates(self):
        """同一 stage 反复失败超 max_retries → escalate。"""
        r = decide_transition(
            gate_decision={"pass": False, "rework_point": "quality"},
            failure_mode="quality",
            history_facts={"failure_count_on_stage": 4, "max_retries": 3},
        )
        assert r["action"] == "escalate"

    def test_first_quality_fail_retries(self):
        """首次 quality fail(无历史成功法)→ retry。"""
        r = decide_transition(
            gate_decision={"pass": False, "rework_point": "quality"},
            failure_mode="quality",
            history_facts={"failure_count_on_stage": 1, "max_retries": 3},
        )
        assert r["action"] == "retry"

    def test_first_build_fail_retries(self):
        r = decide_transition(
            gate_decision={"pass": False, "rework_point": "build"},
            failure_mode="build",
            history_facts={"failure_count_on_stage": 1},
        )
        assert r["action"] == "retry"

    def test_history_swap_succeeded_overrides_repeat_count(self):
        """历史"换法成功"优先于 retry(否则反复 retry 同一失败法)。"""
        r = decide_transition(
            gate_decision={"pass": False},
            failure_mode="tests",
            history_facts={
                "same_failure_swap_succeeded": True,
                "failure_count_on_stage": 2,
                "max_retries": 3,
            },
        )
        assert r["action"] == "swap_approach"

    def test_returns_reason_string(self):
        r = decide_transition(
            gate_decision={"pass": False}, failure_mode="tests", history_facts={}
        )
        assert isinstance(r["reason"], str) and r["reason"]


from story_lifecycle.orchestrator.engine.transition import build_repair_action


class TestBuildRepairAction:
    """decide_transition 决策 → planner 可插入的 action dict(替硬编码 insert)。"""

    def test_retry_yields_verify_repair_with_same_adapter(self):
        r = build_repair_action(
            transition_decision={"action": "retry", "reason": "fix it"},
            story_key="S-1",
            gate_result={"round": 2},
            adapter_name="codex",
        )
        assert r["action"] == "launch"
        assert r["stage"] == "verify"
        assert r["adapter"] == "codex"  # retry 不换 adapter
        assert "round 2" in r["focus"]

    def test_swap_approach_yields_verify_repair_with_different_adapter(self):
        r = build_repair_action(
            transition_decision={"action": "swap_approach", "reason": "history"},
            story_key="S-2",
            gate_result={"round": 1},
            adapter_name="codex",
        )
        assert r["stage"] == "verify"
        assert r["adapter"] != "codex"  # 换 adapter

    def test_insert_rescue_stage_yields_rescue_action(self):
        r = build_repair_action(
            transition_decision={
                "action": "insert_rescue_stage",
                "rescue_stage": "setup_dependency",
                "reason": "缺 dotenv",
            },
            story_key="S-3",
            gate_result={"round": 1},
            adapter_name="claude",
        )
        assert r["stage"] == "setup_dependency"
        assert r["adapter"] == "claude"
        assert "done_file" in r

    def test_escalate_yields_none(self):
        """escalate → 不插 action(caller 标 failed)。"""
        assert (
            build_repair_action(
                transition_decision={"action": "escalate", "reason": "x"},
                story_key="S-4",
                gate_result={"round": 5},
                adapter_name="codex",
            )
            is None
        )

    def test_proceed_yields_none(self):
        assert (
            build_repair_action(
                transition_decision={"action": "proceed", "reason": "ok"},
                story_key="S-5",
                gate_result={},
                adapter_name="codex",
            )
            is None
        )

