# Smart Orchestrator 设计文档

> ⚠️ **历史快照（LangGraph 时代设计，已过时）**：本文描述的编排架构（LangGraph 状态机 / plan_stage / review_stage / run_plan_loop / router 等）已于 cb6f9cd (2026-06-13) 被 Function Calling 模式取代，相应代码已删除或不再接入主流程。本文保留作架构演进决策记录（ADR），**请勿据此理解当前代码**。当前架构见 `design-agent-orchestrator.md`。


## 1. 背景与问题

当前编排器是一个机械的状态机：

```
START → execute_stage → poll_completion → router → advance/retry/skip/fail
```

- `execute_stage` 只是读配置、启 CLI、注入 prompt，没有上下文感知
- `router` 只在出错时才调用 LLM（retry/skip/fail），正常路径直接 advance
- 用户感知不到"智能"——每个阶段独立执行，缺乏跨阶段的连贯性
- Story 结束后知识全部丢失，下次类似需求从零开始

**目标**：让编排 LLM（DeepSeek）充当"规划协调器"，主动指挥其他 CLI 工具，具备长期记忆、结构化反思和上下文压缩能力。

## 2. 设计原则

1. **编排器不做执行**：编排 LLM 只读状态、做决策、下达指令，不写代码
2. **渐进式增强**：没有编排 LLM 时（API Key 未配置），退化为当前行为
3. **指令即文档**：编排器产出的指令写入文件，执行 CLI 从文件读取——这是团队的"共享文档"
4. **已有适配器复用**：`BaseAdapter` / `get_adapter()` 不变，编排器决定用哪个 adapter
5. **索引与详情分离**：State 只存文件路径和摘要，长文本存项目空间文件，防止 Token 膨胀
6. **知识不随会话消亡**：Story 级知识库独立于 State 生命周期，跨 Story 的团队记忆持久存在
7. **事件溯源**：所有动作记录为事件流，状态可回放、可调试

## 3. 角色模型

Smart Orchestrator 采用三角色 SOP，不同角色使用不同 system prompt，甚至不同模型：

| 角色 | 对应节点 | 职责 | 模型 |
|------|----------|------|------|
| **架构师 / PM** | `plan_stage` | 分析需求、拆任务、选工具、写任务书 | DeepSeek（编排 LLM） |
| **QA / 评审员** | `review_stage` | 结构化质量审查、记录问题和建议 | DeepSeek（编排 LLM） |
| **工程师** | `execute_stage` | 读任务书、写代码、产出结果 | Claude / Aider / Codex（执行 CLI） |

角色之间通过**共享文档**（`.story-context/` 和 `.story-knowledge/` 下的文件）协作，而非隐式的 State JSON。工程师不关心 State 细节，只读任务书文件。

## 4. 知识体系

### 4.1 文件布局

```
workspace/
├── .story-context/                    # 阶段级临时上下文（随 stage 生灭）
│   └── {story_key}/
│       ├── plan_design.md             # 架构师给 design 阶段的任务书
│       ├── review_design.md           # QA 对 design 的评审意见
│       ├── plan_implement.md          # 架构师给 implement 阶段的任务书
│       ├── review_implement.md        # QA 对 implement 的评审意见
│       └── ...
├── .story-knowledge/                  # Story 级知识库（长期记忆，Story 完成后保留）
│   └── {story_key}/
│       ├── design.md                  # 需求/设计要点（由 Planner/Reviewer 维护）
│       ├── constraints.md             # 技术约束、边界条件
│       ├── decisions.md               # 关键决策记录（为什么选 A 不选 B）
│       └── compressed.md              # Condenser 压缩后的历史摘要
└── .story-done/                       # 执行 AI 产出（读完即删）

~/.story-lifecycle/
├── knowledge/                         # 跨 Story 的团队记忆（用户级）
│   ├── java-spring-profile.md         # 技术栈通用规范
│   ├── coding-style.md                # 代码风格偏好
│   └── project-{name}.md              # 项目级约定
├── story.db                           # SQLite（含事件流）
└── checkpoint.db                      # LangGraph checkpoint
```

### 4.2 知识库维护

**Story 级知识库**（`.story-knowledge/{story_key}/`）由 Planner 和 Reviewer 在推进过程中自动维护：

```python
# plan_stage_node 中：Planner 发现设计要点时写入知识库
knowledge_dir = Path(workspace) / ".story-knowledge" / story_key
knowledge_dir.mkdir(parents=True, exist_ok=True)

# 设计阶段完成后，review_stage 提取设计要点
design_knowledge = knowledge_dir / "design.md"
design_knowledge.write_text(
    f"# 设计要点: {story_key}\n\n"
    f"## 需求概述\n{plan.get('extra_instructions', '')}\n\n"
    f"## 技术约束\n{review.get('constraints', '')}\n\n"
    f"## 复杂度\n{stage_output.get('complexity', 'M')}",
    encoding="utf-8",
)

# 关键决策记录
decisions_file = knowledge_dir / "decisions.md"
with open(decisions_file, "a", encoding="utf-8") as f:
    f.write(f"\n## {stage} 阶段决策\n")
    f.write(f"- 决策: {plan.get('summary', '')}\n")
    f.write(f"- 理由: {plan.get('reasoning', '')}\n")
    if review.get("issues"):
        for issue in review["issues"]:
            f.write(f"- 问题: {issue['description']} → {issue.get('resolution', '待解决')}\n")
```

**团队记忆**（`~/.story-lifecycle/knowledge/`）由用户手动维护或通过 `story knowledge` 命令生成。Planner 在生成 extra_instructions 时自动检索：

```python
def _load_team_knowledge(workspace: str) -> str:
    """加载团队记忆，注入 Planner prompt。"""
    knowledge_dir = STORY_HOME / "knowledge"
    parts = []
    if knowledge_dir.exists():
        for f in sorted(knowledge_dir.glob("*.md")):
            parts.append(f"### {f.stem}\n{f.read_text(encoding='utf-8')[:500]}")
    return "\n\n".join(parts) if parts else "无"
```

### 4.3 Condenser（上下文压缩）

当 `.story-context/` 下文件过多或 context 过长时，用编排 LLM 压缩历史：

**触发条件**：
- `.story-context/{story_key}/` 下存在超过 4 个文件（2 个已完成阶段的 plan + review）
- 或者 `state["context"]` 条目数超过 10

**压缩流程**：

```python
def compress_context(workspace: str, story_key: str, context: dict) -> str:
    """将多个阶段的历史压缩为一个 knowledge.md。"""
    context_dir = Path(workspace) / ".story-context" / story_key
    files_content = []
    for f in sorted(context_dir.glob("*.md")):
        files_content.append(f"### {f.name}\n{f.read_text(encoding='utf-8')}")

    prompt = f"""将以下多个阶段的历史记录压缩为一个简洁的知识摘要。
保留关键决策、约束和已验证的结论，去除过程细节。

{"".join(files_content)}

输出一个 markdown 文档，包含：已确认的设计决策、技术约束、已完成的产出摘要。"""

    compressed = _call_llm_for_text(prompt)  # 复用编排 LLM

    # 写入知识库
    compressed_file = Path(workspace) / ".story-knowledge" / story_key / "compressed.md"
    compressed_file.parent.mkdir(parents=True, exist_ok=True)
    compressed_file.write_text(compressed, encoding="utf-8")

    # 清理旧的 context 文件（保留最新的 plan + review）
    for f in context_dir.glob("*.md"):
        if f.name != f"plan_{current_stage}.md" and f.name != f"review_{current_stage}.md":
            f.unlink()

    return str(compressed_file.relative_to(workspace))
```

**压缩后**：Planner 只读 `compressed.md` + 当前阶段产出，而不是逐个读所有历史文件。

## 5. 新架构

### 5.1 Graph 变更

```
START → plan_stage ──→ execute_stage → poll_completion → review_stage → router
              │                                                      │
              │                  skip (via conditional edge)         │
              └──── skip_stage ←────────────────────────────────────┘
              ↑                advance                               │
              │← retry ──────── advance                              │
              │                                                       │
              └─── on error: poll 直接跳过 review ──────────→ router
```

新增两个节点：

| 节点 | 角色 | 位置 | 职责 |
|------|------|------|------|
| `plan_stage` | 架构师/PM | execute 之前 | 读上下文索引 + 知识库、决定方案、写任务书文件、维护知识库 |
| `review_stage` | QA/评审员 | poll 之后（仅 happy path） | 结构化质量审查、记录 issues/suggestions、提取知识 |

**关键路由规则**：
- `plan_stage` 通过 conditional edge 决定走向 `execute_stage` 还是 `skip_stage`
- `poll_completion` 发现错误（崩溃/超时）时，设置 `last_error`，直接跳过 `review_stage`
- `review_stage` 仅在 `last_error is None` 时执行（断路器）
- review 判定 `revise` 时受最大重试次数硬限制

### 5.2 State 扩展

```python
class StoryState(TypedDict, total=False):
    # --- 现有字段 ---
    story_key: str
    title: str
    workspace: str
    profile: str
    current_stage: str
    status: str
    complexity: str
    context: dict                # 只存索引（路径 + 一句话摘要）
    execution_count: int
    last_error: Optional[str]
    stage_start_time: float

    # --- 新增字段 ---
    plan_summary: Optional[str]      # 一句话 plan 摘要
    review_summary: Optional[str]    # 一句话 review 结果
    trajectory_score: Optional[float] # 当前路径评分 (0-1)
    _router_decision: Optional[dict]
```

### 5.3 上下文文件持久化

Plan / Review 的详情写入项目空间文件，State 只存索引：

```python
state["context"] = {
    "prd_path": "/path/to/prd.md",
    "spec_path": "/path/to/spec.md",
    "plan_path": ".story-context/STORY-001/plan_implement.md",
    "plan_summary": "根据设计文档实现认证模块，涉及 auth.py 等 3 个文件",
    "review_path": ".story-context/STORY-001/review_implement.md",
    "review_summary": "通过，2 个建议已记录",
    "knowledge_path": ".story-knowledge/STORY-001/compressed.md",
}
```

**执行 AI（Claude Code）** 从 `plan_path` 读取完整任务书。**编排 AI（DeepSeek）** 只看 context 中的索引 + 知识库文件。人类可以直接打开 `.story-context/` 和 `.story-knowledge/` 审查干预。

### 5.4 plan 输出结构

Plan JSON（编排 LLM 原始输出）：

```json
{
  "tool": "stage_tool",
  "args": {
    "adapter": "claude",
    "provider": "deepseek",
    "model": "sonnet",
    "skill": "/brainstorming",
    "instructions_file": ".story-context/STORY-001/plan_design.md"
  },
  "skip": false,
  "summary": "根据设计文档实现认证模块，涉及 auth.py 等 3 个文件",
  "extra_instructions": "根据 docs/design.md 中的方案实现用户认证模块...",
  "reasoning": "设计阶段已完成，spec 明确，直接进入实现",
  "trajectory_score": 0.85
}
```

Plan 任务书文件（写入 `.story-context/{story_key}/plan_{stage}.md`）：

```markdown
# 任务书: {stage}

## 执行指令
根据 docs/design.md 中的方案实现用户认证模块。涉及 auth.py、models.py、api.py 三个文件。
注意设计文档中标注的边界条件处理。

## 前序 Review 建议
- 补充错误处理（auth.py:login 缺少 try/except）
- 添加单元测试覆盖

## 配置
- Adapter: claude
- Provider: deepseek
- Model: sonnet

## 决策理由
设计阶段已完成，spec 明确，直接进入实现。

## 路径评分
当前路径评分: 0.85/1.0 — 方向正确，前序产出质量良好。
```

### 5.5 review 输出结构（结构化反思）

Review 输出升级为结构化格式，包含 issues 列表、建议列表和路径评分：

```json
{
  "quality": "pass",
  "summary": "通过，2 个低优先级建议已记录",
  "feedback": "整体实现完整。建议补充错误处理和单元测试。",
  "issues": [
    {
      "type": "missing_error_handling",
      "severity": "medium",
      "location": "auth.py:login",
      "description": "login 函数缺少网络超时处理"
    }
  ],
  "suggestions": [
    "添加 try/except 处理网络超时",
    "补充 auth.py 的单元测试"
  ],
  "trajectory_score": 0.8,
  "context_updates": {},
  "reasoning": "expected_outputs 全部存在，核心逻辑正确"
}
```

Review 评审文件（写入 `.story-context/{story_key}/review_{stage}.md`）：

```markdown
# 评审: {stage}

## 结论: pass

## 摘要
通过，2 个低优先级建议已记录

## 问题列表
| 类型 | 严重度 | 位置 | 描述 |
|------|--------|------|------|
| missing_error_handling | medium | auth.py:login | login 函数缺少网络超时处理 |

## 建议
- 添加 try/except 处理网络超时
- 补充 auth.py 的单元测试

## 路径评分
0.8/1.0 — 核心逻辑正确，建议补充边界处理。

## 详细理由
expected_outputs 全部存在，核心逻辑正确
```

`quality` 取值：
- `pass` — 质量达标，可以 advance
- `revise` — 需要返工，issues 列表和 suggestions 写入文件，下次 retry 的 plan 会逐条处理
- `fail` — 不可恢复的问题，转 fail_node

## 6. 新模块：`planner.py`

```python
"""Smart Orchestrator — plan and review via LLM.

角色：
- Planner = 架构师/PM（plan_stage）
- Reviewer = QA/评审员（review_stage）
"""

import json
import os
import logging
import httpx
from pathlib import Path

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


def plan_stage(state: dict, stage_config: dict, adapters: list[str]) -> dict:
    """架构师/PM 角色调用：为当前阶段生成执行计划。"""
    api_key, base_url, model = _api_config()
    workspace = state.get("workspace", "")
    story_key = state.get("story_key", "")

    # 加载知识
    team_knowledge = _load_team_knowledge()
    story_knowledge = _load_story_knowledge(workspace, story_key)

    # 前序 review 反馈（结构化 issues）
    previous_review = state.get("review_summary", "")
    retry_hint = ""
    if previous_review and state.get("execution_count", 0) > 0:
        retry_hint = f"""
## 上次 Review 反馈（第 {state['execution_count']} 次重试）
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
{{
  "adapter": "使用哪个 CLI 工具",
  "provider": "使用哪个 provider（或 null）",
  "model": "使用哪个 model（或 null）",
  "skip": false,
  "summary": "一句话摘要（存入 state context）",
  "extra_instructions": "给工程师的详细任务书（写入文件）。要具体、可操作，包含前序阶段的关键信息和团队规范。",
  "reasoning": "决策理由",
  "trajectory_score": 0.85
}}

注意：
- extra_instructions 是给工程师看的任务书，要具体、可操作
- 参考 Story 知识库和团队记忆，让指令更"懂项目"
- summary 是一句话摘要，用于 state context 和 TUI 显示
- trajectory_score 评估当前路径质量 (0-1)，1=完美，0=完全跑偏
- 如果前序 review 有 issues，请在 extra_instructions 中逐条处理
- 如果发现当前阶段不必要，可以 skip: true
- 如果路径评分持续低于 0.5，考虑建议回滚或切换工具"""

    return _call_llm(base_url, api_key, model, prompt)


def review_stage(
    state: dict, stage_config: dict, stage_output: dict
) -> dict:
    """QA/评审员角色调用：结构化审查阶段产出质量。"""
    api_key, base_url, model = _api_config()
    execution_count = state.get("execution_count", 0)
    workspace = state.get("workspace", "")
    story_key = state.get("story_key", "")

    # 加载知识
    story_knowledge = _load_story_knowledge(workspace, story_key)

    # 重试疲劳提示
    fatigue_hint = ""
    if execution_count >= MAX_REVIEW_RETRIES - 1:
        fatigue_hint = f"""
## ⚠️ 重试疲劳警告
该阶段已经重试了 {execution_count} 次，接近 {MAX_REVIEW_RETRIES} 次上限。
如果问题仍然无法解决，请务必返回 quality: "fail"，让人工介入。"""

    # 前序轨迹评分
    prev_score = state.get("trajectory_score")
    score_hint = ""
    if prev_score is not None and prev_score < 0.5:
        score_hint = f"""
## ⚠️ 路径评分偏低
前序阶段路径评分: {prev_score}/1.0。如果当前产出仍未改善，建议 quality: "fail" 以触发重新规划或切换工具。"""

    prompt = f"""你是一个开发团队的 QA/评审员。你的职责是结构化审查产出质量，记录问题和建议。

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
{{
  "quality": "pass|revise|fail",
  "summary": "一句话审查结论（存入 state context）",
  "feedback": "详细审查意见（写入文件）",
  "issues": [
    {{
      "type": "问题类型（如 missing_error_handling, missing_test, wrong_api 等）",
      "severity": "high|medium|low",
      "location": "文件:位置",
      "description": "问题描述"
    }}
  ],
  "suggestions": ["具体改进建议，可操作"],
  "trajectory_score": 0.8,
  "context_updates": {{}},
  "reasoning": "判断理由"
}}

判断标准：
- pass: 产出满足预期，可以 advance。仍可记录低优先级 issues 和 suggestions 供后续参考。
- revise: 产出存在明显缺陷（issues 中至少一个 severity=high），需要返工
- fail: 不可恢复的问题，或已达到重试上限
- trajectory_score: 路径评分 (0-1)，反映从 Story 开始到现在的整体质量趋势
  - 1.0: 完美，一切按预期进行
  - 0.5-0.8: 有小问题但方向正确
  - <0.5: 方向跑偏或质量问题严重，需要重新规划"""

    return _call_llm(base_url, api_key, model, prompt)


def compress_context(workspace: str, story_key: str, current_stage: str) -> str | None:
    """Condenser：将历史 context 文件压缩为知识库摘要。

    触发条件：.story-context/ 下超过 4 个文件。
    返回：压缩文件的相对路径，或 None（不需要压缩）。
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

    # 读取所有历史文件
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

    # 写入知识库
    compressed_file = Path(workspace) / ".story-knowledge" / story_key / "compressed.md"
    compressed_file.parent.mkdir(parents=True, exist_ok=True)
    compressed_file.write_text(compressed, encoding="utf-8")

    # 清理旧 context 文件（保留当前阶段的 plan + review）
    keep = {f"plan_{current_stage}.md", f"review_{current_stage}.md"}
    for f in context_dir.glob("*.md"):
        if f.name not in keep:
            f.unlink()

    return str(compressed_file.relative_to(workspace))


def _call_llm(base_url: str, api_key: str, model: str, prompt: str) -> dict:
    """调用 LLM 并解析 JSON 响应。"""
    resp = httpx.post(
        f"{base_url}/v1/chat/completions",
        headers={"Authorization": f"Bearer {api_key}"},
        json={
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.1,
            "max_tokens": 600,
        },
        timeout=30,
    )
    resp.raise_for_status()
    content = resp.json()["choices"][0]["message"]["content"]

    import re
    m = re.search(r"\{.*\}", content, re.DOTALL)
    if m:
        return json.loads(m.group())
    return json.loads(content)


def _call_llm_for_text(base_url: str, api_key: str, model: str, prompt: str) -> str:
    """调用 LLM 获取文本响应（用于 Condenser）。"""
    resp = httpx.post(
        f"{base_url}/v1/chat/completions",
        headers={"Authorization": f"Bearer {api_key}"},
        json={
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.2,
            "max_tokens": 800,
        },
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"]
```

## 7. Node 变更

### 7.1 `plan_stage_node`（新增）

节点内部绝不调用其他节点函数。skip 通过 conditional edge 路由。

```python
def plan_stage_node(state: StoryState) -> StoryState:
    """架构师/PM 角色：规划当前阶段。无 LLM 时退化为默认 plan。
    
    不调用其他节点。如果 plan 指示 skip，设置状态标记，
    由 graph 的 conditional edge 决定路由到 skip_stage。
    """
    stage = state["current_stage"]
    profile = state.get("profile", "minimal")
    cfg = get_stage_config(profile, stage)
    workspace = state["workspace"]
    story_key = state["story_key"]

    # 触发 Condenser（如果需要）
    compressed_path = planner.compress_context(workspace, story_key, stage)
    if compressed_path:
        state["context"]["knowledge_path"] = compressed_path

    if planner.is_available():
        try:
            adapters = ["claude"]  # 后续从注册表获取
            plan = planner.plan_stage(state, cfg, adapters)

            if plan.get("skip"):
                state["status"] = "skipping"
                state["plan_summary"] = f"跳过: {plan.get('reasoning', '')}"
                log_event(story_key, stage, "plan", {"action": "skip", "reasoning": plan.get("reasoning", "")})
                return state

            # 写 plan 任务书到文件
            plan_file = Path(workspace) / ".story-context" / story_key / f"plan_{stage}.md"
            plan_file.parent.mkdir(parents=True, exist_ok=True)

            # 如果有前序 review issues，在任务书中逐条列出
            review_path = state.get("context", {}).get("review_path")
            review_section = ""
            if review_path:
                rf = Path(workspace) / review_path
                if rf.exists():
                    review_section = f"\n## 前序 Review 建议\n请先处理以下问题：\n{rf.read_text(encoding='utf-8')}"

            plan_file.write_text(
                f"# 任务书: {stage}\n\n"
                f"## 执行指令\n{plan.get('extra_instructions', '')}\n"
                f"{review_section}\n\n"
                f"## 配置\n- Adapter: {plan.get('adapter', 'claude')}\n"
                f"- Provider: {plan.get('provider', 'deepseek')}\n"
                f"- Model: {plan.get('model', 'sonnet')}\n\n"
                f"## 决策理由\n{plan.get('reasoning', '')}\n\n"
                f"## 路径评分\n当前路径评分: {plan.get('trajectory_score', 'N/A')}/1.0",
                encoding="utf-8",
            )

            # State 只存索引
            state["plan_summary"] = plan.get("summary", "")
            state["trajectory_score"] = plan.get("trajectory_score")
            state["context"]["plan_path"] = str(plan_file.relative_to(workspace))
            state["context"]["plan_summary"] = plan.get("summary", "")
            state["plan"] = plan  # 临时持有

            log_event(story_key, stage, "plan", {
                "adapter": plan.get("adapter"),
                "summary": plan.get("summary", "")[:100],
                "trajectory_score": plan.get("trajectory_score"),
            })
            return state
        except Exception as e:
            log.warning(f"Planner failed, falling back: {e}")

    # 退化：用 profile 配置生成 plan
    profile_cfg = load_profile(profile)
    state["plan"] = {
        "adapter": cfg.get("cli", profile_cfg.get("cli", "claude")),
        "provider": state.get("context", {}).get("_provider", cfg.get("provider", "deepseek")),
        "model": cfg.get("model", "sonnet"),
        "skip": False,
        "extra_instructions": "",
        "summary": "Fallback: using profile config",
        "reasoning": "Fallback: using profile config",
        "trajectory_score": None,
    }
    state["plan_summary"] = "Fallback: using profile config"
    return state
```

### 7.2 `execute_stage_node`（修改）

从 plan 读取 adapter/provider，从文件读取任务书：

```python
def execute_stage_node(state: StoryState) -> StoryState:
    key = state["story_key"]
    stage = state["current_stage"]
    workspace = state["workspace"]

    plan = state.get("plan") or {}
    adapter_name = plan.get("adapter", "claude")
    provider = plan.get("provider")
    model = plan.get("model", "sonnet")

    adapter = get_adapter(adapter_name)
    if provider:
        adapter.switch_provider(provider)

    # ... 现有的 ttyd/session 逻辑 ...

    # 渲染 prompt
    prompt = _render_prompt(stage, state)

    # 读取任务书文件并注入
    plan_path = state.get("context", {}).get("plan_path")
    if plan_path:
        plan_file = Path(workspace) / plan_path
        if plan_file.exists():
            plan_content = plan_file.read_text(encoding="utf-8")
            prompt = f"{plan_content}\n\n---\n\n{prompt}"

    # ... 后续逻辑不变 ...

    log_event(key, stage, "execute", {"adapter": adapter_name, "model": model})
```

### 7.3 `review_stage_node`（新增）

断路器 + 重试疲劳 + 结构化反思 + 知识库维护：

```python
def review_stage_node(state: StoryState) -> StoryState:
    """QA/评审员角色：结构化审查阶段产出。仅在 happy path 执行。
    
    断路器：有 last_error 时直接跳过。
    重试疲劳：超过 MAX_REVIEW_RETRIES 次直接 fail。
    """
    if state.get("last_error"):
        return state

    stage = state["current_stage"]
    cfg = get_stage_config(state.get("profile", "minimal"), stage)
    stage_output = state.get("context", {})
    if not stage_output or not cfg.get("expected_outputs"):
        return state

    execution_count = state.get("execution_count", 0)
    if execution_count >= MAX_REVIEW_RETRIES:
        state["last_error"] = f"Review retry limit reached ({MAX_REVIEW_RETRIES} times)"
        state["review_summary"] = f"达到重试上限 ({MAX_REVIEW_RETRIES} 次)"
        log_event(state["story_key"], stage, "review",
                  {"quality": "forced_fail", "retries": execution_count})
        return state

    if planner.is_available():
        try:
            review = planner.review_stage(state, cfg, stage_output)
            workspace = state["workspace"]
            story_key = state["story_key"]

            # 写评审文件
            review_file = Path(workspace) / ".story-context" / story_key / f"review_{stage}.md"
            review_file.parent.mkdir(parents=True, exist_ok=True)

            issues_table = ""
            for issue in review.get("issues", []):
                issues_table += (
                    f"| {issue.get('type', '')} | {issue.get('severity', '')} "
                    f"| {issue.get('location', '')} | {issue.get('description', '')} |\n"
                )

            suggestions_list = "\n".join(f"- {s}" for s in review.get("suggestions", []))

            review_file.write_text(
                f"# 评审: {stage}\n\n"
                f"## 结论: {review.get('quality', 'pass')}\n\n"
                f"## 摘要\n{review.get('summary', '')}\n\n"
                f"## 问题列表\n| 类型 | 严重度 | 位置 | 描述 |\n|------|--------|------|------|\n"
                f"{issues_table or '| （无） | | | |\n'}\n"
                f"## 建议\n{suggestions_list or '（无）'}\n\n"
                f"## 路径评分\n{review.get('trajectory_score', 'N/A')}/1.0\n\n"
                f"## 详细理由\n{review.get('reasoning', '')}",
                encoding="utf-8",
            )

            # State 只存索引
            state["review_summary"] = review.get("summary", "")
            state["trajectory_score"] = review.get("trajectory_score")
            state["context"]["review_path"] = str(review_file.relative_to(workspace))
            state["context"]["review_summary"] = review.get("summary", "")

            # 维护知识库
            _update_knowledge(workspace, story_key, stage, review, stage_output)

            # context_updates 只存索引
            if review.get("context_updates"):
                for k, v in review["context_updates"].items():
                    val = str(v)
                    if len(val) > 200:
                        detail_file = Path(workspace) / ".story-context" / story_key / f"{stage}_{k}.md"
                        detail_file.write_text(val, encoding="utf-8")
                        state["context"][k + "_path"] = str(detail_file.relative_to(workspace))
                        state["context"][k] = val[:100] + "..."
                    else:
                        state["context"][k] = val

            quality = review.get("quality", "pass")
            if quality == "revise":
                high_issues = [i for i in review.get("issues", []) if i.get("severity") == "high"]
                state["last_error"] = (
                    f"Review: {review.get('summary', 'needs revision')} "
                    f"({len(high_issues)} high severity issues)"
                )
            elif quality == "fail":
                state["last_error"] = f"Review failed: {review.get('summary', '')}"

            log_event(story_key, stage, "review", {
                "quality": quality,
                "summary": review.get("summary", "")[:100],
                "issues_count": len(review.get("issues", [])),
                "trajectory_score": review.get("trajectory_score"),
            })
            return state
        except Exception as e:
            log.warning(f"Reviewer failed, skipping review: {e}")

    return state


def _update_knowledge(workspace: str, story_key: str, stage: str,
                      review: dict, stage_output: dict):
    """Reviewer 维护 Story 级知识库。"""
    knowledge_dir = Path(workspace) / ".story-knowledge" / story_key
    knowledge_dir.mkdir(parents=True, exist_ok=True)

    # 设计阶段完成后写 design.md
    if stage == "design" and review.get("quality") == "pass":
        design_file = knowledge_dir / "design.md"
        design_file.write_text(
            f"# 设计要点: {story_key}\n\n"
            f"## 需求概述\n{stage_output.get('summary', '')}\n\n"
            f"## 复杂度\n{stage_output.get('complexity', 'M')}\n\n"
            f"## 技术约束\n{stage_output.get('constraints', '无特殊约束')}",
            encoding="utf-8",
        )

    # 追加决策记录
    decisions_file = knowledge_dir / "decisions.md"
    if not decisions_file.exists():
        decisions_file.write_text(f"# 决策记录: {story_key}\n", encoding="utf-8")

    with open(decisions_file, "a", encoding="utf-8") as f:
        f.write(f"\n## {stage} 阶段\n")
        f.write(f"- 结论: {review.get('summary', '')}\n")
        f.write(f"- 路径评分: {review.get('trajectory_score', 'N/A')}\n")
        for issue in review.get("issues", []):
            f.write(f"- 问题: [{issue.get('severity', '')}] {issue.get('description', '')}"
                    f" @ {issue.get('location', '')}\n")

    # 有 constraints 时单独记录
    constraints = stage_output.get("constraints") or stage_output.get("边界条件")
    if constraints:
        constraints_file = knowledge_dir / "constraints.md"
        existing = constraints_file.read_text(encoding="utf-8") if constraints_file.exists() else ""
        if str(constraints) not in existing:
            with open(constraints_file, "a", encoding="utf-8") as f:
                f.write(f"\n## {stage} 阶段添加\n{constraints}\n")
```

### 7.4 `router_node`（修改）

review 结果 + 轨迹评分参与路由决策：

```python
def router_node(state: StoryState) -> str:
    # 重试疲劳
    review_summary = state.get("review_summary", "")
    if review_summary and "达到重试上限" in review_summary:
        return "fail"

    # 轨迹评分过低 → 回退（不是简单 retry，而是建议重新规划）
    score = state.get("trajectory_score")
    if score is not None and score < 0.3:
        log_event(state["story_key"], state["current_stage"], "router",
                  {"action": "fail", "reason": f"trajectory_score too low: {score}"})
        return "fail"

    # review 发现的问题
    if state.get("last_error") and state.get("review_summary"):
        execution_count = state.get("execution_count", 0)
        if execution_count < MAX_REVIEW_RETRIES:
            return "retry"
        else:
            return "fail"

    # happy path
    if not state.get("last_error"):
        cfg = get_stage_config(...)
        if cfg.get("confirm"):
            return "wait_confirm"
        return "advance"

    # unhappy path — LLM router
    ...
```

### 7.5 路由函数

```python
def route_after_plan(state: StoryState) -> str:
    if state.get("status") == "skipping":
        return "skip_stage"
    return "execute_stage"

def route_after_poll(state: StoryState) -> str:
    if state.get("last_error"):
        return "router"
    return "review_stage"
```

## 8. 事件流（stage_log 升级）

### 8.1 Schema 变更

```sql
-- 原 stage_log
CREATE TABLE stage_log (
    id INTEGER PRIMARY KEY,
    story_id INTEGER REFERENCES story(id),
    stage TEXT,
    action TEXT,           -- enter/complete/skip/retry/fail
    detail TEXT,
    created_at TIMESTAMP
);

-- 升级后 event_log
CREATE TABLE event_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    story_key TEXT NOT NULL,
    stage TEXT NOT NULL,
    event_type TEXT NOT NULL,    -- plan/execute/poll/review/retry/skip/fail/condense
    payload TEXT,                -- JSON（plan/review 内容、错误信息、轨迹评分等）
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
```

### 8.2 事件记录函数

```python
def log_event(story_key: str, stage: str, event_type: str, payload: dict | None = None):
    """记录事件到 event_log。"""
    conn = get_conn()
    conn.execute(
        "INSERT INTO event_log (story_key, stage, event_type, payload) VALUES (?, ?, ?, ?)",
        (story_key, stage, event_type, json.dumps(payload or {}, ensure_ascii=False)),
    )
    conn.commit()
    conn.close()
```

### 8.3 事件类型

| event_type | 触发时机 | payload 示例 |
|------------|----------|-------------|
| `plan` | plan_stage 完成 | `{adapter, summary, trajectory_score}` 或 `{action: "skip", reasoning}` |
| `execute` | execute_stage 启动 CLI | `{adapter, model}` |
| `poll` | poll_completion 发现完成 | `{done_file, parse_result}` |
| `review` | review_stage 完成 | `{quality, summary, issues_count, trajectory_score}` |
| `condense` | Condenser 压缩完成 | `{files_compressed, output_path}` |
| `retry` | router 决定重试 | `{reason, execution_count}` |
| `skip` | 阶段被跳过 | `{reason}` |
| `fail` | 阶段/Story 失败 | `{reason, final_score}` |

### 8.4 TUI 回放

```python
# CLI: story log STORY-001
def show_story_log(story_key: str):
    events = db.query(
        "SELECT * FROM event_log WHERE story_key = ? ORDER BY id", (story_key,)
    )
    for e in events:
        ts = e["created_at"]
        stage = e["stage"]
        etype = e["event_type"]
        payload = json.loads(e["payload"] or "{}")
        summary = payload.get("summary", payload.get("reason", ""))
        score = payload.get("trajectory_score", "")
        score_str = f" (score: {score})" if score else ""
        print(f"  [{ts}] {stage:12s} {etype:8s} {summary}{score_str}")
```

## 9. Graph 变更

```python
def build_graph() -> StateGraph:
    graph = StateGraph(StoryState)

    graph.add_node("plan_stage", plan_stage_node)
    graph.add_node("execute_stage", execute_stage_node)
    graph.add_node("poll_completion", poll_completion_node)
    graph.add_node("review_stage", review_stage_node)
    graph.add_node("router", router_node)
    graph.add_node("advance", advance_node)
    graph.add_node("retry", retry_node)
    graph.add_node("skip_stage", skip_node)
    graph.add_node("fail_stage", fail_node)
    graph.add_node("wait_confirm", wait_confirm_node)

    graph.add_edge(START, "plan_stage")

    graph.add_conditional_edges("plan_stage", route_after_plan, {
        "skip_stage": "skip_stage",
        "execute_stage": "execute_stage",
    })

    graph.add_edge("execute_stage", "poll_completion")

    graph.add_conditional_edges("poll_completion", route_after_poll, {
        "review_stage": "review_stage",
        "router": "router",
    })

    graph.add_edge("review_stage", "router")

    graph.add_conditional_edges("router", router_node, {
        "advance": "advance",
        "retry": "retry",
        "skip": "skip_stage",
        "fail": "fail_stage",
        "wait_confirm": "wait_confirm",
    })

    graph.add_edge("advance", "plan_stage")
    graph.add_edge("retry", "plan_stage")
    graph.add_edge("skip_stage", "advance")
    graph.add_edge("fail_stage", END)
    graph.add_edge("wait_confirm", "plan_stage")

    return graph
```

## 10. 退化策略

| 条件 | 行为 |
|------|------|
| `STORY_LLM_API_KEY` 未设置 | plan/review 跳过 LLM，用 profile cfg 生成默认 plan |
| LLM 调用超时/失败 | 降级为当前行为（直接 execute），log warning |
| plan 输出 JSON 解析失败 | 降级为默认 plan |
| review 输出 JSON 解析失败 | 跳过审查，直接 advance |
| `last_error` 存在（崩溃/超时） | review_stage 完全跳过 |
| review revise 超过 MAX_REVIEW_RETRIES | 硬性断路 fail |
| 轨迹评分 < 0.3 | router 直接 fail，建议重新规划 |
| 团队记忆文件不存在 | Planner prompt 中显示"无团队记忆" |
| Story 知识库为空 | 首次执行正常，无历史参考 |
| Condenser 触发但 LLM 不可用 | 跳过压缩，保留原始文件 |

## 11. 典型流程示例

### 场景 A：正常流程（design → implement）

```
1. advance_node
   → context = {spec_path: "docs/design.md", complexity: "M"}

2. plan_stage (架构师/PM)
   → 读取团队记忆 + Story 知识库（首次为空）
   → 触发 Condenser（不需要，首次）
   → 写入 .story-context/STORY-001/plan_implement.md
   → trajectory_score: 0.85
   → conditional edge → execute_stage

3. execute_stage (工程师)
   → 读取任务书文件，注入到 prompt
   → 启动 Claude Code

4. poll_completion
   → 等待 .story-done/STORY-001/implement.json
   → 无 last_error → review_stage

5. review_stage (QA/评审员)
   → 结构化审查：quality=pass, issues=[], suggestions=[...]
   → trajectory_score: 0.8
   → 写入 review_implement.md + 更新知识库（decisions.md）
   → router → advance

6. plan_stage (for test stage)
   → 读取知识库（已有 design.md + decisions.md）
   → 指令更"懂项目"了
```

### 场景 B：review 发现问题 + 结构化反思

```
5. review_stage
   → quality: revise
   → issues: [{type: "missing_error_handling", severity: "high", location: "auth.py:login"}]
   → suggestions: ["添加 try/except 处理网络超时"]
   → trajectory_score: 0.4（偏低，方向有偏差）

6. router
   → score 0.4 > 0.3，仍在可重试范围
   → retry (execution_count: 1 < 3)

7. plan_stage
   → 看到 execution_count=1，读到 review_path
   → 任务书中逐条列出 issues 和 suggestions
   → extra_instructions 针对 high severity issue 给出具体修复指引
```

### 场景 C：路径评分持续走低 → 回退

```
5. review_stage
   → trajectory_score: 0.2（远低于 0.3 阈值）
   → issues: 多个 high severity

6. router
   → score 0.2 < 0.3 → 直接 fail
   → 记录事件：trajectory_score too low: 0.2
   → 人工介入：可以看知识库了解为什么跑偏
```

### 场景 D：Condenser 触发

```
2. plan_stage (第 3 个阶段 test)
   → .story-context/STORY-001/ 下有 6 个文件（plan + review × 2 + plan_test）
   → 触发 Condenser
   → DeepSeek 把 design + implement 的历史压缩成 compressed.md
   → 清理旧文件（只保留 plan_test.md）
   → 后续 Planner 只读 compressed.md + 当前阶段文件
```

## 12. 文件变更清单

| 文件 | 操作 | 说明 |
|------|------|------|
| `src/story_lifecycle/orchestrator/planner.py` | 新增 | 编排 LLM（plan + review + condense），角色 system prompt，知识库加载 |
| `src/story_lifecycle/orchestrator/nodes.py` | 修改 | 新增 plan/review 节点（断路器 + 知识库维护），修改 execute/router |
| `src/story_lifecycle/orchestrator/graph.py` | 修改 | conditional edges + 新节点 |
| `src/story_lifecycle/orchestrator/router.py` | 保留 | unhappy-path 降级 |
| `src/story_lifecycle/db/models.py` | 修改 | 新增 event_log 表 + log_event 函数 |
| `src/story_lifecycle/cli/main.py` | 修改 | 新增 `story log` 命令（事件回放） |
| `profiles/minimal.yaml` | 可选修改 | 增加 `review: true/false` |
| `.gitignore` | 修改 | 添加 `.story-context/` `.story-knowledge/` |

## 13. 风险

| 风险 | 缓解 |
|------|------|
| 每阶段多 2 次 LLM 调用（plan + review） | fast model + low tokens，约 2-3s/次 |
| DeepSeek API 不稳定 | 退化策略确保核心流程不受影响 |
| plan 任务书与 prompt 模板冲突 | 任务书放在模板 prompt 之前作为"优先指引" |
| review 误判 | 重试疲劳 MAX_REVIEW_RETRIES=3 兜底 |
| context 膨胀 | 索引与详情分离 + Condenser 定期压缩 |
| 轨迹评分不准 | 阈值 0.3 足够保守，不会误杀；持续低分才触发回退 |
| 知识库文件残留 | .gitignore 排除；Story 完成后可归档或保留供后续参考 |
| Condenser 压缩丢信息 | 保留 compressed.md 在知识库，人类可审查；压缩的是过程细节，不是决策 |
| 团队记忆文件越来越多 | 每个文件截断到 500 字符；Planner 只读索引级内容 |
| Tool 注册表膨胀 | 每个 Tool 是独立模块，按需注册；不用的 Tool 不加载 |
| 子 Story 管理复杂度 | 子 Story 共享父 Story 的知识库；生命周期绑定到父 Story |

## 14. Tool 抽象与 Skill 集成

### 14.1 动机

当前 profile 配置 `skill: "/brainstorming"` → `_render_prompt` 写死到 prompt → 执行 AI 调用 skill。这是静态绑定——skill 永远是 profile 里写死的那个。

Planner 应该能根据上下文**动态选择 skill**：设计阶段需要先调研时选 `/research`，需要结构化分析时选 `/brainstorming`，需要代码审查时选 `/code-review`。这需要把"阶段执行"抽象成 Tool，让 Planner 像指挥工程师一样调度不同能力。

### 14.2 Tool Registry

```python
# src/story_lifecycle/orchestrator/tools/__init__.py

from .stage_tool import StageTool
from .skill_tool import SkillTool

TOOLS = {
    "stage_tool": StageTool,      # 标准阶段执行（启动 CLI、跑 skill、等待 .done）
    "skill_tool": SkillTool,       # 纯 skill 执行（不启动完整 CLI，只跑 skill）
}

def get_tool(name: str):
    cls = TOOLS.get(name)
    if not cls:
        raise ValueError(f"Unknown tool: {name}. Available: {list(TOOLS.keys())}")
    return cls()
```

### 14.3 StageTool（标准阶段执行）

现有 `execute_stage_node` 的逻辑封装为 Tool：

```python
# src/story_lifecycle/orchestrator/tools/stage_tool.py

class StageTool:
    """标准阶段执行：启动 CLI adapter，注入任务书 + skill，等待 .done 文件。"""

    def execute(self, state: dict, args: dict) -> dict:
        """
        Args:
            state: 当前 StoryState
            args: {
                adapter: "claude",
                provider: "deepseek",
                model: "sonnet",
                skill: "/brainstorming" (optional),
                instructions_file: ".story-context/STORY-001/plan_design.md"
            }
        Returns:
            更新后的 state（不含 .done 结果，由 poll_completion 处理）
        """
        from ..adapters import get_adapter

        adapter = get_adapter(args.get("adapter", "claude"))
        provider = args.get("provider")
        model = args.get("model", "sonnet")
        skill = args.get("skill")
        instructions_file = args.get("instructions_file")

        if provider:
            adapter.switch_provider(provider)

        # 构建 prompt
        workspace = state["workspace"]
        stage = state["current_stage"]
        prompt = _render_prompt(stage, state)

        # 注入任务书
        if instructions_file:
            plan_file = Path(workspace) / instructions_file
            if plan_file.exists():
                prompt = f"{plan_file.read_text(encoding='utf-8')}\n\n---\n\n{prompt}"

        # 注入 skill 指令
        if skill:
            prompt = f"请先执行 skill: `{skill}` 来进行结构化分析，然后基于分析结果完成本阶段任务。\n\n---\n\n{prompt}"

        # ttyd/session 逻辑（同现有 execute_stage_node）
        # ...

        return state

    def describe(self) -> str:
        return "标准阶段执行：启动 CLI，注入任务书和 skill，等待完成"
```

### 14.4 SkillTool（纯 Skill 执行）

轻量级 skill 调用，不启动完整 CLI 会话：

```python
# src/story_lifecycle/orchestrator/tools/skill_tool.py

class SkillTool:
    """纯 skill 执行：在当前 CLI 会话中执行 skill，不重新启动。

    适用于：不需要完整阶段流程，只需运行特定 skill（如代码审查、性能分析）。
    """

    def execute(self, state: dict, args: dict) -> dict:
        """
        Args:
            state: 当前 StoryState
            args: {
                skill: "/brainstorming",
                instructions_file: ".story-context/STORY-001/plan_design.md",
                adapter: "claude" (optional, 复用已有 session)
            }
        Returns:
            更新后的 state
        """
        workspace = state["workspace"]
        story_key = state["story_key"]
        skill = args.get("skill")
        instructions_file = args.get("instructions_file")

        # 构建轻量 prompt
        prompt = f"执行 skill: `{skill}`\n\n"
        if instructions_file:
            plan_file = Path(workspace) / instructions_file
            if plan_file.exists():
                prompt += f"任务背景：\n{plan_file.read_text(encoding='utf-8')}\n\n"
        prompt += f"完成后写入 .story-done/{story_key}/{state['current_stage']}.json"

        # 复用已有 ttyd session（不创建新 session）
        session = ttyd.session_name(story_key)
        if ttyd.session_alive(session):
            ttyd.send_keys(session, skill, "Enter")
            time.sleep(3)
            ttyd.paste_text(session, prompt)
            ttyd.send_keys(session, "Enter")
        else:
            # fallback 到 StageTool
            return StageTool().execute(state, {
                **args,
                "skill": skill,
            })

        return state

    def describe(self) -> str:
        return "纯 skill 执行：在已有 CLI 会话中运行 skill，不重新启动"
```

### 14.5 未来可扩展的 Tool

| Tool | 用途 | 实现时机 |
|------|------|----------|
| `stage_tool` | 标准阶段执行 | P0（当前） |
| `skill_tool` | 纯 skill 调用 | P0（当前） |
| `research_tool` | 先调研再实现（搜索文档、分析代码库） | P3 |
| `benchmark_tool` | 跑性能测试 | P3 |
| `review_tool` | 专门的代码审查子 Agent | P3 |
| `deploy_tool` | 部署相关操作 | P3 |

### 14.6 Planner 如何选择 Tool

Planner prompt 中列出可用 Tool 及其描述：

```python
def _render_tools_section(tools: dict) -> str:
    parts = []
    for name, tool_cls in tools.items():
        parts.append(f"- **{name}**: {tool_cls().describe()}")
    return "\n".join(parts)
```

Planner 输出中的 `tool` 字段决定使用哪个 Tool，`args` 字段传递参数。`execute_stage_node` 根据 `tool` 分发：

```python
def execute_stage_node(state: StoryState) -> StoryState:
    plan = state.get("plan") or {}
    tool_name = plan.get("tool", "stage_tool")  # 默认退化为 stage_tool
    tool_args = plan.get("args", {})

    # 退化：旧格式 plan 没有 tool 字段
    if not tool_args and "adapter" in plan:
        tool_args = {
            "adapter": plan["adapter"],
            "provider": plan.get("provider"),
            "model": plan.get("model", "sonnet"),
            "skill": plan.get("skill"),
            "instructions_file": state.get("context", {}).get("plan_path"),
        }

    tool = get_tool(tool_name)
    return tool.execute(state, tool_args)
```

**向后兼容**：旧格式 plan（没有 `tool` 字段）自动包装为 `stage_tool` 调用。退化模式下 profile 配置仍然有效。

### 14.7 Skill 动态选择

Planner 可以根据上下文动态选择 skill，而不是依赖 profile 静态配置：

```python
# planner.py 中 plan_stage prompt 追加
## 可用 Skill（执行 AI 支持的技能）
{json.dumps(available_skills)}

你可以在 args.skill 中指定一个 skill 让工程师执行。选择依据：
- 设计阶段 → /brainstorming（结构化分析）或 /research（先调研）
- 实现阶段 → 无需 skill（直接写代码）或 /tdd（TDD 模式）
- 测试阶段 → 无需 skill（直接跑测试）
- 代码审查 → /code-review（专门的审查 skill）

如果不确定需要哪个 skill，可以不指定（args.skill 设为 null）。
```

```python
# 可用 skill 列表（从配置或发现获取）
available_skills = [
    "/brainstorming",
    "/research",
    "/tdd",
    "/code-review",
    "/orchestrate",
]
```

## 15. 子 Story 委派

### 15.1 动机

大 Story（L 复杂度）涉及多个服务/模块，单次 CLI 执行无法覆盖。Planner 应该能像项目经理一样拆分任务，让多个"工程师"并行工作。

### 15.2 子 Story 模型

```
STORY-001 (父 Story, L 复杂度)
├── STORY-001-auth    (子 Story: 认证模块)
├── STORY-001-api     (子 Story: API 层)
└── STORY-001-db      (子 Story: 数据库迁移)
```

每个子 Story 有自己的 LangGraph thread，但共享父 Story 的知识库：

```
workspace/.story-knowledge/
├── STORY-001/              # 父 Story 知识库
│   ├── design.md           # 整体设计
│   ├── constraints.md      # 全局约束
│   └── decisions.md        # 决策记录
├── STORY-001-auth/         # 子 Story 知识库（独立但可读父级）
│   └── decisions.md
└── STORY-001-api/
    └── decisions.md
```

### 15.3 数据模型

```sql
ALTER TABLE story ADD COLUMN parent_key TEXT;  -- 父 Story key（null 表示顶层 Story）
ALTER TABLE story ADD COLUMN subtask_index INTEGER DEFAULT 0;  -- 子任务序号
```

### 15.4 Planner 拆分决策

Planner 在 `plan_stage` 中可以决定拆分：

```json
{
  "tool": "stage_tool",
  "args": { ... },
  "split": true,
  "subtasks": [
    {
      "key_suffix": "auth",
      "title": "实现认证模块",
      "summary": "根据设计文档实现 auth.py、models.py",
      "depends_on": []
    },
    {
      "key_suffix": "api",
      "title": "实现 API 层",
      "summary": "根据设计文档实现 api.py 路由",
      "depends_on": ["auth"]
    }
  ]
}
```

### 15.5 子 Story 执行流程

```python
def plan_stage_node(state: StoryState) -> StoryState:
    # ... 现有逻辑 ...

    plan = planner.plan_stage(state, cfg, adapters)

    # Planner 决定拆分
    if plan.get("split") and plan.get("subtasks"):
        return _delegate_subtasks(state, plan)

    # 不拆分：正常执行
    # ...


def _delegate_subtasks(parent_state: StoryState, plan: dict) -> StoryState:
    """创建子 Story 并提交执行。父 Story 暂停等待。"""
    parent_key = parent_state["story_key"]
    workspace = parent_state["workspace"]
    profile = parent_state.get("profile", "minimal")

    for i, sub in enumerate(plan["subtasks"]):
        sub_key = f"{parent_key}-{sub['key_suffix']}"

        # 创建子 Story（共享父知识库）
        create_and_start_story(
            story_key=sub_key,
            title=sub["title"],
            profile=profile,
            workspace=workspace,
        )

        # 子 Story 继承父知识库
        sub_knowledge = Path(workspace) / ".story-knowledge" / sub_key
        sub_knowledge.mkdir(parents=True, exist_ok=True)
        # 符号链接到父知识库（或直接读取父级）
        parent_knowledge = Path(workspace) / ".story-knowledge" / parent_key
        for f in parent_knowledge.glob("*.md"):
            (sub_knowledge / f.name).symlink_to(f)

        # 写子任务 plan 文件
        plan_file = Path(workspace) / ".story-context" / sub_key / f"plan_implement.md"
        plan_file.parent.mkdir(parents=True, exist_ok=True)
        plan_file.write_text(
            f"# 子任务: {sub['title']}\n\n"
            f"## 所属 Story\n{parent_key} 的子任务 ({i+1}/{len(plan['subtasks'])})\n\n"
            f"## 执行指令\n{sub.get('summary', '')}\n\n"
            f"## 约束\n这是子任务，只负责本模块的实现，不要修改其他模块。",
            encoding="utf-8",
        )

        # 启动子 Story
        start_story_async(sub_key)

        log_event(parent_key, parent_state["current_stage"], "delegate", {
            "sub_key": sub_key,
            "title": sub["title"],
            "depends_on": sub.get("depends_on", []),
        })

    # 父 Story 暂停，等待子 Story 完成
    parent_state["status"] = "waiting_subtasks"
    db.update_story(parent_key, status="waiting_subtasks")
    return parent_state
```

### 15.6 子 Story 完成检测

Watchdog 扫描时检测子 Story 完成状态：

```python
async def watchdog_check(self):
    # 检查 waiting_subtasks 的父 Story
    parents = db.query(
        "SELECT * FROM story WHERE status = 'waiting_subtasks'"
    )
    for parent in parents:
        parent_key = parent["story_key"]
        children = db.query(
            "SELECT * FROM story WHERE parent_key = ? AND status != 'completed'",
            (parent_key,),
        )
        if not children:
            # 所有子 Story 完成 → 恢复父 Story
            db.update_story(parent_key, status="active")
            resume_story(parent_key)
            log_event(parent_key, "", "subtasks_completed", {
                "parent_key": parent_key,
            })
```

### 15.7 依赖顺序

子 Story 的 `depends_on` 控制执行顺序：

```python
def _delegate_subtasks(parent_state, plan):
    subtasks = plan["subtasks"]
    for sub in subtasks:
        deps = sub.get("depends_on", [])
        if deps:
            # 等待依赖完成后再创建
            # 简化实现：串行创建有依赖的子任务
            sub["status"] = "waiting_deps"
        create_and_start_story(...)
```

### 15.8 TUI 展示

```
┌─ Story Board ──────────────────────────────────────────────┐
│ STORY-001  用户认证系统                    [waiting subs]   │
│   └─ STORY-001-auth  认证模块              [active]         │
│   └─ STORY-001-api   API 层                [blocked: dep]  │
│   └─ STORY-001-db    数据库迁移             [active]         │
└─────────────────────────────────────────────────────────────┘
```

## 16. 更新后的文件变更清单

| 文件 | 操作 | 说明 |
|------|------|------|
| `src/story_lifecycle/orchestrator/planner.py` | 新增 | 编排 LLM（plan + review + condense），角色 system prompt，知识库加载 |
| `src/story_lifecycle/orchestrator/nodes.py` | 修改 | 新增 plan/review 节点，execute 按 tool 分发，子 Story 委派 |
| `src/story_lifecycle/orchestrator/graph.py` | 修改 | conditional edges + 新节点 |
| `src/story_lifecycle/orchestrator/router.py` | 保留 | unhappy-path 降级 |
| `src/story_lifecycle/orchestrator/tools/__init__.py` | 新增 | Tool 注册表 |
| `src/story_lifecycle/orchestrator/tools/stage_tool.py` | 新增 | 标准阶段执行 Tool |
| `src/story_lifecycle/orchestrator/tools/skill_tool.py` | 新增 | 纯 skill 调用 Tool |
| `src/story_lifecycle/db/models.py` | 修改 | event_log 表 + log_event + parent_key 字段 |
| `src/story_lifecycle/cli/main.py` | 修改 | `story log` 命令 |
| `profiles/minimal.yaml` | 可选修改 | `review: true/false` |
| `.gitignore` | 修改 | `.story-context/` `.story-knowledge/` |

## 17. 实现分期

| 阶段 | 内容 | 依赖 |
|------|------|------|
| **Phase 1** | Smart Orchestrator 核心（planner.py + plan/review 节点 + 断路器 + 重试疲劳） | 无 |
| **Phase 1** | Tool 抽象基础（stage_tool + skill_tool） | Smart Orchestrator |
| **Phase 1** | 事件流（event_log + log_event + story log 命令） | 无 |
| **Phase 2** | 知识体系（.story-knowledge/ + 团队记忆 + 知识库自动维护） | Smart Orchestrator |
| **Phase 2** | 结构化反思（issues[] + suggestions[] + trajectory_score） | Smart Orchestrator |
| **Phase 2** | Condenser（上下文压缩） | 知识体系 |
| **Phase 3** | 子 Story 委派（split + delegate + subtask tracking） | 知识体系 + Tool 抽象 |
| **Phase 3** | 扩展 Tool（research_tool / benchmark_tool / review_tool） | Tool 抽象 |
