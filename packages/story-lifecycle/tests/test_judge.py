"""Tests for quality judge Decider(层4 质量评判)。

``judge_quality`` 纯 Decider:
``(done_data, test_result, story_facts, llm_invoke) -> {pass, rework_point?, reason}``。
先规则判硬指标(build/tests 失败 → 直接 rework,不调 LLM);硬指标过 → LLM judge
结构化输出(选项固定 [pass, rework],守 §2.2 #5)。零副作用,LLM 注入。
"""

from story_lifecycle.orchestrator.evaluation.judge import judge_quality


def _ok_done():
    return {"build_passed": True, "tests_passed": True}


class TestJudgeQuality:
    def test_build_failed_rework_without_llm(self):
        """done 标 build_passed=False → 直接 rework "build",不调 LLM(省 token)。"""
        calls = {"n": 0}

        def fake_llm(prompt):
            calls["n"] += 1
            return '{"choice": "pass", "reason": "x"}'

        r = judge_quality(
            done_data={"build_passed": False, "tests_passed": True},
            test_result={},
            story_facts={"story_key": "S-1", "stage": "implement"},
            llm_invoke=fake_llm,
        )
        assert r["pass"] is False
        assert r["rework_point"] == "build"
        assert calls["n"] == 0  # 硬指标 fail 不调 LLM

    def test_tests_failed_rework_without_llm(self):
        calls = {"n": 0}

        def fake_llm(prompt):
            calls["n"] += 1
            return '{"choice": "pass", "reason": "x"}'

        r = judge_quality(
            done_data={"build_passed": True, "tests_passed": False},
            test_result={},
            story_facts={"story_key": "S-2", "stage": "implement"},
            llm_invoke=fake_llm,
        )
        assert r["pass"] is False
        assert r["rework_point"] == "tests"
        assert calls["n"] == 0

    def test_test_result_failures_rework(self):
        """test_result 带 failures → rework "tests"(即使 done 自报 tests_passed=True)。"""
        calls = {"n": 0}

        def fake_llm(p):
            calls["n"] += 1
            return '{"choice":"pass","reason":"x"}'

        r = judge_quality(
            done_data=_ok_done(),
            test_result={"failures": ["test_login", "test_signup"], "passed": 5, "failed": 2},
            story_facts={"story_key": "S-3", "stage": "verify"},
            llm_invoke=fake_llm,
        )
        assert r["pass"] is False
        assert r["rework_point"] == "tests"
        assert "test_login" in r["reason"] or "2" in r["reason"]
        assert calls["n"] == 0

    def test_all_ok_llm_says_pass(self):
        def fake_llm(prompt):
            assert "pass" in prompt or "rework" in prompt or "质量" in prompt  # prompt 合理
            return '{"choice": "pass", "reason": "实现符合验收标准"}'

        r = judge_quality(
            done_data=_ok_done(),
            test_result={"passed": 10, "failed": 0},
            story_facts={"story_key": "S-4", "stage": "implement", "summary": "add notif"},
            llm_invoke=fake_llm,
        )
        assert r["pass"] is True
        assert "rework_point" not in r or r["rework_point"] is None
        assert "验收" in r["reason"]

    def test_all_ok_llm_says_rework(self):
        def fake_llm(prompt):
            return '{"choice": "rework", "reason": "缺少错误处理"}'

        r = judge_quality(
            done_data=_ok_done(),
            test_result={"passed": 10, "failed": 0},
            story_facts={"story_key": "S-5", "stage": "implement"},
            llm_invoke=fake_llm,
        )
        assert r["pass"] is False
        assert r["rework_point"] == "quality"
        assert "错误处理" in r["reason"]

    def test_missing_done_fields_default_safe_and_llm_judges(self):
        """done_data 缺字段(没自报 build/tests)→ 不误判 fail,交 LLM judge。"""
        calls = {"n": 0}

        def fake_llm(p):
            calls["n"] += 1
            return '{"choice":"pass","reason":"ok"}'

        r = judge_quality(
            done_data={},  # 没 build_passed/tests_passed
            test_result={},
            story_facts={"story_key": "S-6", "stage": "implement"},
            llm_invoke=fake_llm,
        )
        assert r["pass"] is True
        assert calls["n"] == 1  # 交 LLM 判
