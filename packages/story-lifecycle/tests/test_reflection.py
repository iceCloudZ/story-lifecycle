"""Tests for reflection Decider(层5 反思学习)。

``reflect`` 读决策事件流(supervisor_decision / recovery_action / judge_verdict 等)→
沉淀可复用 ``playbook``。**verifier 形态**:基于事件 ground truth(recovery 后是否真 pass),
非 verbal reflection(LLM 自我对话易自欺,§2.2 #6)。纯函数,零副作用。

这是飞轮的"反思→知识"环节:跑 N story 后,把"换 adapter 解过同类失败"这类经验
沉淀成规则,供层2 transition 的 history_facts / context_providers 回注(新 story 受益)。
"""

from story_lifecycle.orchestrator.learning.reflection import reflect


def ev(event_type, story_key, **payload):
    return {"event_type": event_type, "story_key": story_key, "payload": payload}


class TestReflect:
    def test_empty_events_empty_playbook(self):
        r = reflect(events=[])
        assert r["playbook"] == []
        assert r["stats"] == {}

    def test_swap_followed_by_pass_yields_playbook_rule(self):
        """recovery_action 换 adapter,后续同 story pass → playbook 记"换法成功"。"""
        events = [
            ev("recovery_action", "S-1", action="retry_new_adapter",
               failed_adapter="codex", new_adapter="claude"),
            ev("judge_verdict", "S-1", passed=True),
        ]
        r = reflect(events=events)
        assert len(r["playbook"]) == 1
        rule = r["playbook"][0]
        assert "codex" in rule["rule"] and "claude" in rule["rule"]
        assert rule["support"] == 1

    def test_multiple_supporting_instances_increase_support(self):
        """多个 story 都出现"codex→claude 换成功"→ support 累加,排前。"""
        events = []
        for s in ("S-1", "S-2", "S-3"):
            events += [
                ev("recovery_action", s, action="retry_new_adapter",
                   failed_adapter="codex", new_adapter="claude"),
                ev("judge_verdict", s, passed=True),
            ]
        # 一个不相关的弱证据
        events += [
            ev("recovery_action", "S-9", action="retry_new_adapter",
               failed_adapter="claude", new_adapter="kimi"),
            ev("judge_verdict", "S-9", passed=True),
        ]
        r = reflect(events=events)
        top = r["playbook"][0]
        assert "codex" in top["rule"] and "claude" in top["rule"]
        assert top["support"] == 3  # 三例支撑,排第一

    def test_swap_not_followed_by_pass_is_not_evidence(self):
        """recovery 换了 adapter 但 story 没 pass → 不沉淀(避免学错)。"""
        events = [
            ev("recovery_action", "S-1", action="retry_new_adapter",
               failed_adapter="codex", new_adapter="claude"),
            # 没有 pass 事件(仍失败 / 卡住)
        ]
        r = reflect(events=events)
        assert r["playbook"] == []

    def test_stats_counts_event_types(self):
        events = [
            ev("supervisor_decision", "S-1", choice="A"),
            ev("supervisor_decision", "S-1", choice="B"),
            ev("recovery_action", "S-1", action="retry_new_adapter"),
        ]
        r = reflect(events=events)
        assert r["stats"]["supervisor_decision"] == 2
        assert r["stats"]["recovery_action"] == 1

    def test_recovery_without_retry_new_adapter_ignored(self):
        """escalate_human / skip_stage 类 recovery 不算换法证据。"""
        events = [
            ev("recovery_action", "S-1", action="escalate_human"),
            ev("judge_verdict", "S-1", passed=True),
        ]
        r = reflect(events=events)
        assert r["playbook"] == []


from story_lifecycle.orchestrator.learning.reflection import (
    build_transition_history_facts,
)


class TestBuildTransitionHistoryFacts:
    """层5 回注:reflect 的 playbook → transition 的 history_facts(让 swap_approach 真触发)。

    飞轮:recovery 换 adapter 成功 → reflect 沉淀"X 失败→换 Y 成功" → 本函数把它变成
    transition 的 ``same_failure_swap_succeeded`` → decide_transition 返回 swap_approach。
    """

    def test_matching_playbook_rule_sets_swap_succeeded(self):
        """历史有"codex 失败→换 claude 成功"→ 当前 codex 失败时 swap_succeeded=True。"""
        events = [
            ev("recovery_action", "S-x", action="retry_new_adapter",
               failed_adapter="codex", new_adapter="claude"),
            ev("judge_verdict", "S-x", passed=True),
        ]
        hf = build_transition_history_facts(
            events=events, failed_adapter="codex", gate_round=1, retry_limit=3
        )
        assert hf["same_failure_swap_succeeded"] is True
        assert hf["failure_count_on_stage"] == 1
        assert hf["max_retries"] == 3

    def test_no_matching_rule_leaves_swap_false(self):
        """历史只解过 codex→claude,当前 kimi 失败 → swap_succeeded=False。"""
        events = [
            ev("recovery_action", "S-x", action="retry_new_adapter",
               failed_adapter="codex", new_adapter="claude"),
            ev("judge_verdict", "S-x", passed=True),
        ]
        hf = build_transition_history_facts(
            events=events, failed_adapter="kimi", gate_round=1, retry_limit=3
        )
        assert hf["same_failure_swap_succeeded"] is False

    def test_empty_events_defaults(self):
        hf = build_transition_history_facts(
            events=[], failed_adapter="codex", gate_round=2, retry_limit=3
        )
        assert hf["same_failure_swap_succeeded"] is False
        assert hf["failure_count_on_stage"] == 2

    def test_feeds_decide_transition_to_swap_approach(self):
        """端到端:history_facts 让 decide_transition 返回 swap_approach(非 retry)。"""
        from story_lifecycle.orchestrator.engine.transition import decide_transition

        events = [
            ev("recovery_action", "S-x", action="retry_new_adapter",
               failed_adapter="codex", new_adapter="claude"),
            ev("judge_verdict", "S-x", passed=True),
        ]
        hf = build_transition_history_facts(
            events=events, failed_adapter="codex", gate_round=1, retry_limit=3
        )
        decision = decide_transition(
            gate_decision={"pass": False, "rework_point": "verify"},
            failure_mode="quality",
            history_facts=hf,
        )
        assert decision["action"] == "swap_approach"

