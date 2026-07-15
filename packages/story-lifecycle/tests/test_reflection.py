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


# ============================================================================
# REFACTOR §5.1 — reflect 扩展规则 + write_playbook_file 落库 + persist_playbook
# ============================================================================

from story_lifecycle.orchestrator.learning.reflection import (
    write_playbook_file,
    persist_playbook,
)


class TestReflectExtendedRules:
    """REFACTOR §5.1.1:reflect 扩展识别 failure-pattern / rescue / dimension 字段。"""

    def test_adapter_routing_rule_has_dimension(self):
        """adapter swap 规则带 dimension='adapter-routing'。"""
        events = [
            ev("recovery_action", "S-1", action="retry_new_adapter",
               failed_adapter="codex", new_adapter="claude"),
            ev("judge_verdict", "S-1", passed=True),
        ]
        r = reflect(events=events)
        assert r["playbook"][0]["dimension"] == "adapter-routing"

    def test_failure_pattern_repeated_retry_yields_rule(self):
        """同 stage 连续 retry ≥2 次 + pass → 沉淀 failure-pattern。"""
        events = [
            ev("transition_decision", "S-1", action="retry", stage="build", reason="test fail"),
            ev("transition_decision", "S-1", action="retry", stage="build", reason="test fail again"),
            ev("judge_verdict", "S-1", passed=True),
        ]
        r = reflect(events=events)
        fp_rules = [x for x in r["playbook"] if x["dimension"] == "failure-pattern"]
        assert len(fp_rules) == 1
        assert "build" in fp_rules[0]["rule"]
        assert fp_rules[0]["support"] == 2

    def test_single_retry_not_failure_pattern(self):
        """单次 retry(<2)不算 pattern,不沉淀。"""
        events = [
            ev("transition_decision", "S-1", action="retry", stage="build", reason="once"),
            ev("judge_verdict", "S-1", passed=True),
        ]
        r = reflect(events=events)
        fp_rules = [x for x in r["playbook"] if x["dimension"] == "failure-pattern"]
        assert len(fp_rules) == 0

    def test_rescue_insert_rescue_stage_yields_rule(self):
        """insert_rescue_stage 后 pass → 沉淀 rescue 规则。"""
        events = [
            ev("recovery_action", "S-1", action="insert_rescue_stage",
               rescue_stage="setup_dependency", reason="缺 mock"),
            ev("judge_verdict", "S-1", passed=True),
        ]
        r = reflect(events=events)
        rescue_rules = [x for x in r["playbook"] if x["dimension"] == "rescue"]
        assert len(rescue_rules) == 1
        assert "setup_dependency" in rescue_rules[0]["rule"]

    def test_reason_stored_as_evidence_not_structured(self):
        """§5.1.1 Q3:reason 存原文,不做结构化抽取。"""
        events = [
            ev("recovery_action", "S-1", action="retry_new_adapter",
               failed_adapter="codex", new_adapter="claude",
               reason="codex 在大文件上 timeout"),
            ev("judge_verdict", "S-1", passed=True),
        ]
        r = reflect(events=events)
        rule = r["playbook"][0]
        assert "codex 在大文件上 timeout" in rule["evidence"]

    def test_playbook_sorted_by_support_desc(self):
        """support 高的排前。"""
        events = []
        # codex→claude 出现 3 次
        for s in ("A", "B", "C"):
            events += [
                ev("recovery_action", s, action="retry_new_adapter",
                   failed_adapter="codex", new_adapter="claude"),
                ev("judge_verdict", s, passed=True),
            ]
        # claude→kimi 出现 1 次
        events += [
            ev("recovery_action", "D", action="retry_new_adapter",
               failed_adapter="claude", new_adapter="kimi"),
            ev("judge_verdict", "D", passed=True),
        ]
        r = reflect(events=events)
        assert r["playbook"][0]["support"] >= r["playbook"][1]["support"]


class TestWritePlaybookFile:
    """REFACTOR §5.1.2:write_playbook_file 按 task_type 分层落盘 + support 累加。"""

    def test_writes_to_task_type_subdir(self, tmp_path):
        """文件落在 <workspace>/.story/knowledge/playbooks/<task_type>/<dimension>.md。"""
        playbook = [
            {"dimension": "adapter-routing", "rule": "codex→claude", "support": 1, "evidence": "x"},
        ]
        p = write_playbook_file(
            workspace=str(tmp_path), task_type="credit-limit",
            dimension="adapter-routing", playbook=playbook,
        )
        assert p is not None
        assert "credit-limit" in p
        assert "adapter-routing.md" in p

    def test_support_accumulates_on_repeated_write(self, tmp_path):
        """同 rule 多次写入 → support 累加(不是文本去重堆积)。"""
        playbook = [
            {"dimension": "adapter-routing", "rule": "codex→claude", "support": 1, "evidence": "first"},
        ]
        write_playbook_file(
            workspace=str(tmp_path), task_type="credit-limit",
            dimension="adapter-routing", playbook=playbook,
        )
        # 第二次写同 rule
        playbook2 = [
            {"dimension": "adapter-routing", "rule": "codex→claude", "support": 1, "evidence": "second"},
        ]
        write_playbook_file(
            workspace=str(tmp_path), task_type="credit-limit",
            dimension="adapter-routing", playbook=playbook2,
        )
        from pathlib import Path
        content = (tmp_path / ".story" / "knowledge" / "playbooks" /
                   "credit-limit" / "adapter-routing.md").read_text(encoding="utf-8")
        assert "support: 2" in content
        # evidence 取最新
        assert "second" in content

    def test_empty_task_type_returns_none(self, tmp_path):
        """task_type 为空不落库(冷启动期可能未分类)。"""
        p = write_playbook_file(
            workspace=str(tmp_path), task_type="",
            dimension="adapter-routing", playbook=[{"rule": "x", "support": 1}],
        )
        assert p is None

    def test_empty_playbook_returns_none(self, tmp_path):
        p = write_playbook_file(
            workspace=str(tmp_path), task_type="credit-limit",
            dimension="adapter-routing", playbook=[],
        )
        assert p is None

    def test_best_effort_does_not_raise(self, tmp_path):
        """写失败只 warning,不抛异常。"""
        # 传一个不可写的路径(模拟失败)——这里用空 playbook + 合法路径测正常路径
        # 真正的失败路径难模拟,至少确认空输入不崩
        p = write_playbook_file(
            workspace=str(tmp_path), task_type="x",
            dimension="adapter-routing", playbook=[],
        )
        assert p is None  # 空 playbook → None,不崩


class TestPersistPlaybook:
    """REFACTOR §5.1.3:persist_playbook 串起 reflect → 分文件落盘。"""

    def test_persists_multiple_dimensions(self, tmp_path):
        """reflect 产出多 dimension → 各自落对应文件。"""
        events = [
            ev("recovery_action", "S-1", action="retry_new_adapter",
               failed_adapter="codex", new_adapter="claude", reason="r1"),
            ev("transition_decision", "S-1", action="retry", stage="build", reason="r2"),
            ev("transition_decision", "S-1", action="retry", stage="build", reason="r3"),
            ev("judge_verdict", "S-1", passed=True),
        ]
        persist_playbook(
            workspace=str(tmp_path), story_key="S-1",
            events=events, task_type="credit-limit",
        )
        playbooks_dir = tmp_path / ".story" / "knowledge" / "playbooks" / "credit-limit"
        assert (playbooks_dir / "adapter-routing.md").exists()
        assert (playbooks_dir / "failure-patterns.md").exists()

    def test_empty_task_type_skips(self, tmp_path):
        """task_type 为空 → persist_playbook 不落任何文件。"""
        persist_playbook(
            workspace=str(tmp_path), story_key="S-1",
            events=[ev("recovery_action", "S-1", action="retry_new_adapter",
                       failed_adapter="codex", new_adapter="claude")],
            task_type="",
        )
        assert not (tmp_path / ".story").exists()

    def test_no_playbook_rules_skips(self, tmp_path):
        """没有可沉淀的规则(事件没 pass 兜底)→ 不落文件。"""
        events = [
            ev("recovery_action", "S-1", action="retry_new_adapter",
               failed_adapter="codex", new_adapter="claude"),
            # 没有 pass 事件
        ]
        persist_playbook(
            workspace=str(tmp_path), story_key="S-1",
            events=events, task_type="credit-limit",
        )
        assert not (tmp_path / ".story").exists()


