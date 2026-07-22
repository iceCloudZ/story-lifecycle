"""story consult —— code agent 的求助通道(CLI 一次性进程)。

code agent(headless stage 内)用 Bash 工具调 ``story consult`` → 编排 LLM FC loop
(可 spawn 外援 CLI 实地调查)→ advisory 打印 stdout。**永远 exit 0**(除用法错误
exit 2),绝不阻塞 code agent(DESIGN §5.2)。

env(由 planner spawn headless stage 时注入,DESIGN §5.8):
- STORY_KEY / STORY_STAGE / STORY_WORKSPACE / STORY_ADAPTER —— 必需
- STORY_CONSULT_DEPTH —— 递归守卫(≥1 拒绝;外援 spawn 时注入 1,DESIGN §5.5)
- STORY_CONSULT_FAKE —— 测试缝(设置后跳过真 LLM/spawn,直接打印其值)

分层(AGENTS.md):
- **纯核心** ``run_consult_cli``:全注入(env/orchestrator/log_event 都是参数),
  可单测,无 IO 副作用依赖(os.environ 之类由调用方注入)。
- **click 薄壳** ``consult_cmd``:读 argv/env,落 DB 事件,打印,exit。测试缝
  (STORY_CONSULT_FAKE)只在这一层接线,核心不感知。

详细设计见 packages/story-lifecycle/docs/DESIGN-consult-tool.md。
"""

from __future__ import annotations

import uuid
from typing import Callable

import click


def run_consult_cli(
    *,
    question: str,
    context: str,
    urgency: str,
    env: dict,
    # 注入点(测试用)
    log_event_fn: Callable,
    run_consult_orchestrator_fn: Callable,
    id_factory: Callable[[], str] | None = None,
) -> tuple[str, int]:
    """处理一次 consult 调用 → (stdout 文本, exit code)。

    纯核心层:无 IO 副作用,所有外部依赖(env / log_event / orchestrator)都注入。

    Returns:
        ``(text, 0)`` —— 正常或降级(fallback advisory 也返 0,不阻塞 code agent)。
        ``(text, 2)`` —— 用法错误(env 缺失 / depth 守卫命中),text 为原因。
    """
    # 递归守卫:外援不可再 consult(DESIGN §5.5)
    depth_raw = env.get("STORY_CONSULT_DEPTH", "0") or "0"
    try:
        depth = int(depth_raw)
    except (ValueError, TypeError):
        depth = 0
    if depth >= 1:
        return (
            "consult: reviewer 不可再 consult(递归守卫)。把不确定性写进 findings。",
            2,
        )

    story_key = env.get("STORY_KEY", "")
    workspace = env.get("STORY_WORKSPACE", "")
    if not story_key or not workspace:
        return (
            "consult: 缺 STORY_KEY/STORY_WORKSPACE —— 只能在 story headless stage 内调用。",
            2,
        )

    rid = (id_factory or (lambda: uuid.uuid4().hex[:12]))()
    stage = env.get("STORY_STAGE", "unknown")
    adapter_of_caller = env.get("STORY_ADAPTER", "")

    consult_request = {
        "request_id": rid,
        "question": question,
        "context": context,
        "urgency": urgency,
        "adapter_of_caller": adapter_of_caller,
    }
    log_event_fn(story_key, stage, "consult_request", consult_request)

    # 故事事实(可扩展:加 recent_events / open_findings / task_type)
    story_facts = {
        "story_key": story_key,
        "stage": stage,
        # TODO(后续): 从 DB 取 task_type / recent events 摘要 / open findings
    }

    try:
        result = run_consult_orchestrator_fn(
            consult_request=consult_request,
            story_facts=story_facts,
            workspace=workspace,
        )
    except Exception as exc:
        result = {
            "advice": f"(consult 异常: {exc}. 请自行决断)",
            "confidence": "low",
            "terminated_by": "exception",
        }

    # consult_response 事件 —— 去掉 spawn_results(可能含大量 findings),保留计数
    response_payload = {
        "id": rid,
        **{k: v for k, v in result.items() if k != "spawn_results"},
        "spawn_count": len(result.get("spawn_results", [])),
    }
    log_event_fn(story_key, stage, "consult_response", response_payload)

    advice_text = result.get("advice", "")
    confidence = result.get("confidence", "unknown")
    return (f"[consult {rid}] [confidence: {confidence}]\n{advice_text}", 0)


# ── click 薄壳(同 calendar_cmd / list_cmd 模式)─────────────────────


@click.command("consult")
@click.option("--question", required=True, help="具体问题")
@click.option(
    "--context", default="", help="上下文(长文本建议用 --context-file)"
)
@click.option(
    "--context-file",
    "context_file",
    default="",
    help="上下文文件路径(优先于 --context,推荐)",
)
@click.option(
    "--urgency",
    type=click.Choice(["low", "medium", "high"]),
    default="medium",
)
def consult_cmd(question, context, context_file, urgency):
    """向编排层 LLM 求助(可 spawn 外援 CLI 调查)。供 headless stage 内的 code agent 调用。"""
    import os
    from pathlib import Path

    if context_file:
        context = Path(context_file).read_text(encoding="utf-8")

    # 测试缝(DESIGN §8.2):fake 模式跳过真 LLM + 真 spawn,事件仍正常落
    fake = os.environ.get("STORY_CONSULT_FAKE")
    if fake:
        def _orch_fn(**kw):
            return {
                "advice": fake,
                "confidence": "high",
                "followed_up": False,
                "rounds": 0,
                "terminated_by": "test_fake",
                "spawn_results": [],
            }
    else:
        def _orch_fn(**kw):
            from ...orchestrator.engine.consult_orchestrator import (
                run_consult_orchestrator,
            )
            from ...orchestrator.engine.consult_runner import run_consult_sync
            from ...infra.llm_client import get_llm

            return run_consult_orchestrator(
                invoke_with_tools=get_llm().invoke_with_tools,
                spawn_fn=run_consult_sync,
                **kw,
            )

    text, code = run_consult_cli(
        question=question,
        context=context,
        urgency=urgency,
        env=dict(os.environ),
        log_event_fn=_safe_log_event,
        run_consult_orchestrator_fn=_orch_fn,
    )
    click.echo(text)
    raise SystemExit(code)


def _safe_log_event(story_key, stage, event_type, payload):
    """落 DB 事件,best-effort(对齐 clarify_server._emit 风格)。

    失败不抛 —— consult 的事件落 DB 是可观测性,不是核心路径。落 DB 失败时
    code agent 仍能拿到 advisory(更重要的产出)。
    """
    try:
        from ...infra.db import models as db

        db.log_event(story_key, stage, event_type, payload)
    except Exception:
        pass


__all__ = ["consult_cmd", "run_consult_cli"]
