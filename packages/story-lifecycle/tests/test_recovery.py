"""Tests for recovery Decider(层3 失败恢复)。

``decide_recovery`` 是纯 Decider:
``(exc, story_facts, adapter, attempt_count, recovery_facts) -> {action, reason, new_adapter?}``。
零副作用,规则驱动(策略确定;LLM/policy_engine 可后置扩展)。守 §2.2 #1。

action 取值:
- ``retry_new_adapter``:瞬时错,未达上限 → 换 adapter 重试(带 new_adapter)。
- ``escalate_human``:auth/config 错,或高价值 story 反复失败 → 上交人。
- ``downgrade_to_manual``:中价值 story 达上限 → 降级人工接手。
- ``skip_stage``:低价值 story 达上限 → 跳过该 stage。
- ``abort``:policy_engine 判定彻底无解(本基础版不主动触发)。
"""

import pytest

from story_lifecycle.orchestrator.engine.recovery import decide_recovery


def _facts(key="S", priority="P2"):
    return {"story_key": key, "stage": "implement", "priority": priority}


class TestDecideRecovery:
    def test_transient_first_attempt_retries_on_new_adapter(self):
        """瞬时错(timeout / done-never)首次 → retry_new_adapter,换 adapter。"""
        r = decide_recovery(
            exc=TimeoutError("done file never appeared"),
            story_facts=_facts("S-1", "P2"),
            adapter="codex",
            attempt_count=1,
        )
        assert r["action"] == "retry_new_adapter"
        assert r["new_adapter"] != "codex"
        assert isinstance(r["new_adapter"], str)

    def test_auth_config_error_escalates_to_human(self):
        """auth/config 类错误(无 LLM 也能判)→ escalate_human,不浪费重试。"""
        for msg in ("API key not configured", "401 unauthorized", "cloud config bundle"):
            r = decide_recovery(
                exc=RuntimeError(msg),
                story_facts=_facts("S-2"),
                adapter="claude",
                attempt_count=1,
            )
            assert r["action"] == "escalate_human", f"failed for msg={msg!r}"

    def test_max_attempts_high_priority_escalates(self):
        r = decide_recovery(
            exc=TimeoutError("stuck"),
            story_facts=_facts("S-3", "P0"),
            adapter="codex",
            attempt_count=3,
            recovery_facts={"max_attempts": 3},
        )
        assert r["action"] == "escalate_human"

    def test_max_attempts_mid_priority_downgrades_to_manual(self):
        r = decide_recovery(
            exc=TimeoutError("stuck"),
            story_facts=_facts("S-4", "P2"),
            adapter="codex",
            attempt_count=3,
            recovery_facts={"max_attempts": 3},
        )
        assert r["action"] == "downgrade_to_manual"

    def test_max_attempts_low_priority_skips_stage(self):
        r = decide_recovery(
            exc=TimeoutError("stuck"),
            story_facts=_facts("S-5", "P4"),
            adapter="codex",
            attempt_count=3,
            recovery_facts={"max_attempts": 3},
        )
        assert r["action"] == "skip_stage"

    def test_new_adapter_cycles_through_order(self):
        """按 adapter_order 轮转:codex→claude→kimi。"""
        order = ["codex", "claude", "kimi"]
        r1 = decide_recovery(
            exc=TimeoutError("x"),
            story_facts=_facts("S", "P2"),
            adapter="codex",
            attempt_count=1,
            recovery_facts={"adapter_order": order},
        )
        assert r1["new_adapter"] == "claude"
        r2 = decide_recovery(
            exc=TimeoutError("x"),
            story_facts=_facts("S", "P2"),
            adapter="claude",
            attempt_count=2,
            recovery_facts={"adapter_order": order},
        )
        assert r2["new_adapter"] == "kimi"
        r3 = decide_recovery(
            exc=TimeoutError("x"),
            story_facts=_facts("S", "P2"),
            adapter="kimi",
            attempt_count=3,
            recovery_facts={"adapter_order": order, "max_attempts": 99},
        )
        assert r3["new_adapter"] == "codex"  # 回绕

    def test_unknown_adapter_falls_back_to_first(self):
        """adapter 不在 order → 用 order[0]。"""
        r = decide_recovery(
            exc=TimeoutError("x"),
            story_facts=_facts("S", "P2"),
            adapter="wat",
            attempt_count=1,
            recovery_facts={"adapter_order": ["codex", "kimi"]},
        )
        assert r["new_adapter"] == "codex"

    def test_always_returns_reason_string(self):
        r = decide_recovery(
            exc=RuntimeError("boom"),
            story_facts=_facts("S", "P2"),
            adapter="codex",
            attempt_count=1,
        )
        assert isinstance(r["reason"], str) and r["reason"]
