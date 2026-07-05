"""Quality judge Decider(层4 质量评判)。

``judge_quality`` 决定一个 stage 的产出是否合格:不合格给 ``rework_point`` 让层2
transition 决定返工/换法。两段决策:
1. **硬指标(规则,无 LLM)**:done 的 ``build_passed`` / ``tests_passed`` 为 False,或
   ``test_result`` 有 failures → 直接 rework(省 token;§2.2 #7)。
2. **LLM judge(结构化)**:硬指标过 → 喂结构化 facts(done + test_result + story),
   固定选项 [pass, rework](§2.2 #5),复用 ``supervisor.decide_response`` 决策大脑。

**纯 Decider(§2.2 #1)**:零副作用,LLM 通过 ``llm_invoke`` 注入。Handler(gate)接决策
落 ``judge_verdict`` 事件 + 触发 rework/放行。

gate 接入:协调 AI-2 窗口(gate decide+apply 拆分)——gate decide 完调本 Decider,
fail 则不 apply / 触发 retry(本阶段最小判据:done 字段空/false → rework)。
"""

from __future__ import annotations

from typing import Optional


def judge_quality(
    *,
    done_data: dict,
    test_result: dict,
    story_facts: dict,
    llm_invoke,
) -> dict:
    """Pure Decider. Judge whether a stage's output is acceptable.

    Args:
        done_data: done handshake payload. Honored keys: ``build_passed``,
            ``tests_passed`` (missing → treated as not-yet-failed, falls through to LLM).
        test_result: real test run summary. Honored keys: ``failures`` (list),
            ``failed`` (int).
        story_facts: structured story context.
        llm_invoke: injected LLM call for the quality-judge step (prompt -> JSON str).

    Returns:
        ``{"pass": bool, "reason": str, "rework_point"?: str}``.
        ``rework_point`` only present (non-None) when ``pass`` is False.
    """
    test_result = test_result or {}

    # (1) 硬指标:done 自报 build/tests 失败 → 直接 rework,不调 LLM
    if done_data.get("build_passed") is False:
        return {
            "pass": False,
            "rework_point": "build",
            "reason": "done 自报 build_passed=False → 构建未过",
        }
    failures = list(test_result.get("failures") or [])
    failed_count = test_result.get("failed", 0) or 0
    if done_data.get("tests_passed") is False or failures or failed_count:
        detail = ", ".join(failures) if failures else f"{failed_count} 个失败"
        return {
            "pass": False,
            "rework_point": "tests",
            "reason": f"测试未过:{detail}",
        }

    # (2) 硬指标过 → LLM judge(结构化,固定选项 [pass, rework])
    from ..engine.supervisor import decide_response

    decision = decide_response(
        question=_build_judge_prompt(done_data, test_result, story_facts),
        options=["pass", "rework"],
        story_facts=story_facts,
        llm_invoke=llm_invoke,
    )
    if decision["choice"] == "rework":
        return {
            "pass": False,
            "rework_point": "quality",
            "reason": decision["reason"],
        }
    return {"pass": True, "reason": decision["reason"], "rework_point": None}


def _build_judge_prompt(done_data: dict, test_result: dict, story_facts: dict) -> str:
    """结构化 facts(LangGraph 范式),不喂原始日志(降噪,§2.2 #4)。"""
    import json

    return (
        "你是 stage 产出质量评判器。基于结构化结果判定实现是否合格。\n"
        f"Story 上下文: {json.dumps(story_facts, ensure_ascii=False)}\n"
        f"Done 数据: {json.dumps(done_data, ensure_ascii=False)}\n"
        f"测试结果: {json.dumps(test_result, ensure_ascii=False)}\n"
        "硬指标(build/tests)已过;判实现质量是否达标(正确性/完整性/可维护性)。\n"
        "只返回 JSON,不要任何额外文字:\n"
        ' {"choice": "<pass 或 rework>", "reason": "<简短理由>"}'
    )
