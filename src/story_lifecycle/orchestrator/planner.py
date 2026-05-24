"""Smart Orchestrator — plan and review via LLM.

Role-based prompts:
- Planner = 架构师/PM (plan_stage)
- Reviewer = QA/评审员 (review_stage)
"""

import json
import os
import logging
import re
import time
from pathlib import Path

import httpx

log = logging.getLogger("story-lifecycle.planner")

STORY_HOME = Path.home() / ".story-lifecycle"
MAX_REVIEW_RETRIES = 3


def _api_config() -> tuple[str, str, str]:
    """Return (api_key, base_url, model)."""
    return (
        os.environ.get("STORY_LLM_API_KEY", ""),
        os.environ.get("STORY_LLM_BASE_URL", "https://api.deepseek.com"),
        os.environ.get("STORY_LLM_MODEL", "deepseek-chat"),
    )


def is_available() -> bool:
    return bool(_api_config()[0])


def _load_team_knowledge() -> str:
    """加载团队记忆（~/.story-lifecycle/knowledge/）。"""
    knowledge_dir = STORY_HOME / "knowledge"
    parts = []
    if knowledge_dir.exists():
        for f in sorted(knowledge_dir.glob("*.md")):
            content = f.read_text(encoding="utf-8")[:500]
            parts.append(f"### {f.stem}\n{content}")
    return "\n\n".join(parts) if parts else "（无团队记忆）"


def _load_story_knowledge(workspace: str, story_key: str) -> str:
    """加载 Story 级知识库。"""
    knowledge_dir = Path(workspace) / ".story-knowledge" / story_key
    parts = []
    if knowledge_dir.exists():
        for f in sorted(knowledge_dir.glob("*.md")):
            content = f.read_text(encoding="utf-8")[:800]
            parts.append(f"### {f.stem}\n{content}")
    return "\n\n".join(parts) if parts else "（无 Story 知识）"


def plan_stage(
    state: dict,
    stage_config: dict,
    adapters: list[str],
) -> dict:
    """架构师/PM 角色：为当前阶段生成执行计划。"""
    api_key, base_url, model = _api_config()
    workspace = state.get("workspace", "")
    story_key = state.get("story_key", "")

    team_knowledge = _load_team_knowledge()
    story_knowledge = _load_story_knowledge(workspace, story_key)

    previous_review = state.get("review_summary", "")
    retry_hint = ""
    if previous_review and state.get("execution_count", 0) > 0:
        retry_hint = f"""
## 上次 Review 反馈（第 {state["execution_count"]} 次重试）
{previous_review}
请针对上述反馈逐条调整执行指令。"""

    prompt = f"""你是一个开发团队的架构师/项目经理。你的职责是分析需求、规划任务、给工程师下达明确的任务书。

你管理一个 Story 的生命周期。Story 即将进入新阶段。

## Story 信息
- Key: {state.get("story_key")}
- 标题: {state.get("title")}
- 当前阶段: {state.get("current_stage")}
- 已重试次数: {state.get("execution_count", 0)}
- 阶段描述: {stage_config.get("description", "")}

## 已有上下文索引（路径 + 摘要）
{json.dumps(state.get("context", {}), ensure_ascii=False, indent=2)}

## Story 知识库
{story_knowledge}

## 团队记忆
{team_knowledge}
{retry_hint}

## 可用 CLI 工具
{json.dumps(adapters)}

## 阶段配置
{json.dumps(stage_config, ensure_ascii=False, indent=2)}

请决定执行方案。返回 JSON：
{{{{
  "adapter": "使用哪个 CLI 工具",
  "provider": "使用哪个 provider（或 null）",
  "model": "使用哪个 model（或 null）",
  "skip": false,
  "summary": "一句话摘要（存入 state context）",
  "extra_instructions": "给工程师的详细任务书（写入文件）。要具体、可操作，包含前序阶段的关键信息和团队规范。",
  "reasoning": "决策理由",
  "trajectory_score": 0.85
}}}}

## 子 Story 拆分（可选）
如果这个 Story 的复杂度为 L，或者涉及多个独立模块，你可以选择拆分为子 Story。
在返回的 JSON 中加入：
{{{{
  "split": true,
  "subtasks": [
    {{{{ "key_suffix": "auth", "title": "实现认证模块", "summary": "根据设计文档实现 auth.py", "depends_on": [] }}}},
    {{{{ "key_suffix": "api", "title": "实现 API 层", "summary": "实现 api.py 路由", "depends_on": ["auth"] }}}}
  ]
}}}}
不拆分时不要包含 split 和 subtasks 字段。子 Story 共享父 Story 知识库，各自独立执行。
depends_on 填写其他 subtask 的 key_suffix，控制执行顺序（有依赖的子任务等前置完成后才启动）。

注意：
- extra_instructions 是给工程师看的任务书，要具体、可操作
- 参考 Story 知识库和团队记忆，让指令更"懂项目"
- summary 是一句话摘要，用于 state context 和 TUI 显示
- trajectory_score 评估当前路径质量 (0-1)，1=完美，0=完全跑偏
- 如果前序 review 有 issues，请在 extra_instructions 中逐条处理
- 如果发现当前阶段不必要，可以 skip: true
- 如果路径评分持续低于 0.5，考虑建议回滚或切换工具"""

    return _call_llm(
        base_url,
        api_key,
        model,
        prompt,
        story_key=state.get("story_key", ""),
        stage=state.get("current_stage", ""),
    )


def review_stage(
    state: dict, stage_config: dict, stage_output: dict, *, reviewer_model: str = ""
) -> dict:
    """QA/评审员角色：结构化审查阶段产出质量。"""
    api_key, base_url, model = _api_config()
    if reviewer_model:
        model = reviewer_model
    execution_count = state.get("execution_count", 0)
    workspace = state.get("workspace", "")
    story_key = state.get("story_key", "")

    story_knowledge = _load_story_knowledge(workspace, story_key)

    fatigue_hint = ""
    if execution_count >= MAX_REVIEW_RETRIES - 1:
        fatigue_hint = f"""
## ⚠️ 重试疲劳警告
该阶段已经重试了 {execution_count} 次，接近 {MAX_REVIEW_RETRIES} 次上限。
如果问题仍然无法解决，请务必返回 quality: "fail"，让人工介入。"""

    prev_score = state.get("trajectory_score")
    score_hint = ""
    if prev_score is not None and prev_score < 0.5:
        score_hint = f"""
## ⚠️ 路径评分偏低
前序阶段路径评分: {prev_score}/1.0。如果当前产出仍未改善，建议 quality: "fail" 以触发重新规划或切换工具。"""

    prompt = f"""你是一个开发团队的 QA/评审员。你是评审员，只读不改——你不修改任何代码或文件，只负责审查、记录问题和建议。

一个阶段刚刚完成，请进行质量审查。

## Story 信息
- Key: {state.get("story_key")}
- 阶段: {state.get("current_stage")}
- 已重试次数: {execution_count} / {MAX_REVIEW_RETRIES}
- 阶段描述: {stage_config.get("description", "")}

## 阶段产出
{json.dumps(stage_output, ensure_ascii=False, indent=2)}

## 预期产出字段
{json.dumps(stage_config.get("expected_outputs", []))}

## 已有上下文索引
{json.dumps(state.get("context", {}), ensure_ascii=False, indent=2)}

## Story 知识库
{story_knowledge}
{fatigue_hint}
{score_hint}

请审查产出质量。返回 JSON：
{{{{
  "quality": "pass|revise|fail",
  "summary": "一句话审查结论（存入 state context）",
  "feedback": "详细审查意见（写入文件）",
  "issues": [
    {{{{
      "type": "问题类型（如 missing_error_handling, missing_test, wrong_api 等）",
      "severity": "high|medium|low",
      "location": "文件:位置",
      "description": "问题描述"
    }}}}
  ],
  "suggestions": ["具体改进建议，可操作"],
  "trajectory_score": 0.8,
  "context_updates": {{{{}}}},
  "reasoning": "判断理由"
}}}}

判断标准：
- pass: 产出满足预期，可以 advance。仍可记录低优先级 issues 和 suggestions 供后续参考。
- revise: 产出存在明显缺陷（issues 中至少一个 severity=high），需要返工
- fail: 不可恢复的问题，或已达到重试上限
- trajectory_score: 路径评分 (0-1)，反映从 Story 开始到现在的整体质量趋势
  - 1.0: 完美，一切按预期进行
  - 0.5-0.8: 有小问题但方向正确
  - <0.5: 方向跑偏或质量问题严重，需要重新规划"""

    return _call_llm(
        base_url,
        api_key,
        model,
        prompt,
        story_key=state.get("story_key", ""),
        stage=state.get("current_stage", ""),
    )


def review_plan(
    state: dict,
    plan: dict,
    stage_config: dict,
    reviewer_model: str = "",
) -> dict:
    """Plan Reviewer 角色：对执行计划进行对抗性审查。"""
    api_key, base_url, model = _api_config()
    if reviewer_model:
        model = reviewer_model

    workspace = state.get("workspace", "")
    story_key = state.get("story_key", "")

    story_knowledge = _load_story_knowledge(workspace, story_key)

    prompt = f"""你是一个开发团队的技术评审员，专门负责审查执行计划的质量。你的职责是确保计划具备足够的范围覆盖、上下文完整性和可行性。

一份执行计划刚刚生成，请进行质量审查。

## Story 信息
- Key: {state.get("story_key")}
- 标题: {state.get("title")}
- 当前阶段: {state.get("current_stage")}
- 阶段描述: {stage_config.get("description", "")}

## 执行计划
{json.dumps(plan, ensure_ascii=False, indent=2)}

## 已有上下文索引
{json.dumps(state.get("context", {}), ensure_ascii=False, indent=2)}

## Story 知识库
{story_knowledge}

请审查计划质量。返回 JSON：
{{{{
  "quality": "pass|revise",
  "blockers": [
    {{{{
      "severity": "high|medium|low",
      "category": "scope|context|feasibility",
      "description": "问题描述"
    }}}}
  ],
  "suggestions": ["具体改进建议，可操作"],
  "reasoning": "判断理由"
}}}}

判断标准：
- pass: 计划范围合理、指令具体明确、与知识库对齐，可以执行
- revise: 计划存在严重问题（blockers 中至少一个 severity=high），需要重新生成
  - scope 问题：计划范围过大或过小，遗漏关键步骤
  - context 问题：计划缺少必要的前序上下文或团队规范
  - feasibility 问题：计划中包含不可行的技术方案或不存在的工具/接口

注意：
- 只关注严重问题（severity=high），中等和低等问题记入 suggestions 即可
- 不要因为风格偏好或非关键细节而触发 revise
- 优先检查：adapter 是否有效、extra_instructions 是否具体可操作、是否遗漏 stage_config 要求的步骤"""

    return _call_llm(
        base_url,
        api_key,
        model,
        prompt,
        story_key=state.get("story_key", ""),
        stage=state.get("current_stage", ""),
    )


def compress_context(workspace: str, story_key: str, current_stage: str) -> str | None:
    """Condenser：将历史 context 文件压缩为知识库摘要。

    触发条件：.story-context/ 下超过 4 个文件。
    """
    context_dir = Path(workspace) / ".story-context" / story_key
    if not context_dir.exists():
        return None

    files = sorted(context_dir.glob("*.md"))
    if len(files) <= 4:
        return None

    api_key, base_url, model = _api_config()
    if not api_key:
        return None

    history_parts = []
    for f in files:
        content = f.read_text(encoding="utf-8")
        history_parts.append(f"### {f.name}\n{content}")

    prompt = f"""将以下多个阶段的历史记录压缩为一个简洁的知识摘要。
保留关键决策、约束、已验证的结论和未解决的问题。
去除过程细节（如 adapter 选择、model 配置等）。

{"".join(history_parts)}

输出 markdown，包含：
- 已确认的设计决策
- 技术约束和边界条件
- 已完成产出的摘要
- 未解决的问题（如有）"""

    compressed = _call_llm_for_text(base_url, api_key, model, prompt)

    compressed_file = Path(workspace) / ".story-knowledge" / story_key / "compressed.md"
    compressed_file.parent.mkdir(parents=True, exist_ok=True)
    compressed_file.write_text(compressed, encoding="utf-8")

    # Archive old files instead of deleting
    keep = {f"plan_{current_stage}.md", f"review_{current_stage}.md"}
    archive = context_dir / "archive"
    archive.mkdir(exist_ok=True)
    import shutil

    for f in context_dir.glob("*.md"):
        if f.name not in keep:
            shutil.move(str(f), str(archive / f.name))

    return str(compressed_file.relative_to(workspace))


def _call_llm(
    base_url: str,
    api_key: str,
    model: str,
    prompt: str,
    *,
    story_key: str = "",
    stage: str = "",
) -> dict:
    """调用 LLM 并解析 JSON 响应。"""
    t0 = time.monotonic()
    try:
        resp = httpx.post(
            f"{base_url}/v1/chat/completions",
            headers={"Authorization": f"Bearer {api_key}"},
            json={
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.1,
            },
            timeout=90,
        )
        resp.raise_for_status()
        body = resp.json()
        msg = body["choices"][0]["message"]
        content = msg.get("content", "") or msg.get("reasoning_content", "")
        usage = body.get("usage", {})
        _trace_llm(
            model=model,
            usage=usage,
            duration_ms=int((time.monotonic() - t0) * 1000),
            story_key=story_key,
            stage=stage,
        )
        if not content.strip():
            raise RuntimeError(
                "LLM returned empty content — reasoning model may have exhausted tokens."
            )
        return _parse_llm_response(content)
    except Exception as exc:
        _trace_llm(
            model=model,
            usage={},
            duration_ms=int((time.monotonic() - t0) * 1000),
            success=False,
            error=type(exc).__name__,
            story_key=story_key,
            stage=stage,
        )
        raise


def _trace_llm(
    *,
    model: str,
    usage: dict,
    duration_ms: int,
    operation: str = "plan_stage",
    story_key: str = "",
    stage: str = "",
    success: bool = True,
    error: str = "",
):
    """Record LLM call trace to DB."""
    try:
        from ..db.models import log_llm_trace

        log_llm_trace(
            story_key=story_key,
            stage=stage,
            operation=operation,
            model=model,
            prompt_tokens=usage.get("prompt_tokens", 0),
            completion_tokens=usage.get("completion_tokens", 0),
            total_tokens=usage.get("total_tokens", 0),
            duration_ms=duration_ms,
            success=success,
            error=error,
        )
    except Exception:
        pass


def _parse_llm_response(content: str) -> dict:
    m = re.search(r"\{.*\}", content, re.DOTALL)
    if m:
        return json.loads(m.group())
    return json.loads(content)


def _stream_llm(
    base_url: str,
    api_key: str,
    model: str,
    prompt: str,
    on_chunk,
) -> dict:
    """流式调用 LLM，实时回调 on_chunk，返回解析后的 JSON。"""
    full: list[str] = []
    with httpx.stream(
        "POST",
        f"{base_url}/v1/chat/completions",
        headers={"Authorization": f"Bearer {api_key}"},
        json={
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.1,
            "stream": True,
        },
        timeout=90,
    ) as resp:
        resp.raise_for_status()
        for raw_line in resp.iter_lines():
            line = raw_line.decode("utf-8") if isinstance(raw_line, bytes) else raw_line
            if not line.startswith("data: "):
                continue
            data = line[6:]
            if data == "[DONE]":
                break
            try:
                chunk = json.loads(data)
                delta = chunk["choices"][0]["delta"].get("content", "")
                if delta:
                    full.append(delta)
                    on_chunk(delta)
            except (json.JSONDecodeError, KeyError):
                pass
    return _parse_llm_response("".join(full))


def _call_llm_for_text(base_url: str, api_key: str, model: str, prompt: str) -> str:
    """调用 LLM 获取文本响应（用于 Condenser）。"""
    t0 = time.monotonic()
    try:
        resp = httpx.post(
            f"{base_url}/v1/chat/completions",
            headers={"Authorization": f"Bearer {api_key}"},
            json={
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.2,
            },
            timeout=90,
        )
        resp.raise_for_status()
        body = resp.json()
        msg = body["choices"][0]["message"]
        usage = body.get("usage", {})
        _trace_llm(
            model=model,
            usage=usage,
            duration_ms=int((time.monotonic() - t0) * 1000),
        )
        return msg.get("content", "") or msg.get("reasoning_content", "")
    except Exception as exc:
        _trace_llm(
            model=model,
            usage={},
            duration_ms=int((time.monotonic() - t0) * 1000),
            success=False,
            error=type(exc).__name__,
        )
        raise
