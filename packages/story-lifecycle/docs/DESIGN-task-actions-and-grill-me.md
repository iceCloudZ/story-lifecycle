# 动作清单 + grill-me — 设计文档

> 状态:待评审。创建:2026-07-15。
> 范围:`packages/story-lifecycle`(尤其 `orchestrator/engine/planner.py` 的 prompt 组装)。
> 评审目标:验证"LLM 从动作库选任务"的方案正确性 + grill-me 中断/resume 路径的可行性。
> 本文自包含:背景、代码现状、方案、grill-me 方向全部内联。

---

## 0. TL;DR(评审者先读)

当前编排器给 CLI 的提示词靠 **stage 名隐含该干什么**(design=设计、build=编码、verify=验证)。这导致:

1. **single-pass 单阶段被卡住**——stage 名是 verify,拿到"禁止跑测试"的约束,但它本该全干
2. **不同 task_type 没法适配**——纯前端和后端该强调的东西不同,但提示词一样
3. **"one prompt fits all"是 myth**——学术界已有定论(见 §2)

方案:**把"stage 该干什么"从隐含改成显式**——编排器 LLM 在规划时,从一个预制的**动作库**里为每个 stage 选该做哪些活(写设计文档/改代码/跑测试/验收/写报告),拼成该 stage 的任务清单。CLI 拿到清单知道干几件事,不再靠 stage 名猜。

**边界**:动作库是编排层预制的(LLM 只选不编);完成协议/done 格式/工具引导仍硬编码(第②层不动)。

**grill-me(下一步,本文档只设计不实现)**:设计阶段不该无脑往下走,该有追问拉扯(grill-me)——CLI 提问、中断等人答、resume 继续。复用现有 clarify/interactive_pty 机制。

---

## 1. 起因与背景

### 1.1 触发事件

新增 `single-pass` profile(单阶段全干,REFACTOR §5.0)后,发现 verify stage 的 prompt 拿到了"不要运行耗时构建/测试命令"的执行约束。这个约束对多阶段模式的 verify 合理(它只验证,测试归 build),但 **single-pass 是单 CLI 长跑自己 design+build+verify 全干**,被这条约束卡住了——它没法自验。

根因:`_build_cli_prompt` 里的执行约束是按 `if stage == "verify"` / `if _is_single_stage` 硬编码的(见 §3 代码现状),不看任务性质。

### 1.2 问题的本质

**stage 名不该决定该干什么活。** 同样叫"verify"的 stage:
- 多阶段模式:只做验证(跑测试 + 验收)
- 单阶段模式:什么都干(调研 + 设计 + 编码 + 测试 + 验收 + 出报告)
- 不同 task_type:可能侧重不同(前端重样式验收,后端重事务验证)

靠 stage 名隐含该干什么,**本质是"one prompt fits all"**——用同一个提示词模板套所有场景。

### 1.3 这是编排器-模型的"信息差"体现

编排器持有 profile + 需求 + task_type + 历史经验,它比 stage 名更懂"这个任务该干哪些活"。让它显式选动作清单,是信息差护城河的又一次兑现(见 REFACTOR-orchestrator-three-layer-positioning.md §2.1)。

---

## 2. 理论依据

### 2.1 "one prompt fits all" 是 myth

[ML Pills routing 文章](https://mlpills.substack.com/p/diy-20-routing-llm-agent-with-langchain)明确指出:

> "one prompt fits all" is a myth

不同任务/场景该用不同提示词,靠一个模板套所有是已知的反模式。

### 2.2 Meta-Prompting(LLM 组装 sub-prompt)

[arXiv:2312.06562 "On Meta-Prompting"](https://arxiv.org/html/2312.06562v4):

> 一个 meta-prompter LLM 组装多个 sub-prompt,做 conditional instruction injection(上下文相关指令注入)。

本文方案的"动作清单"正是 conditional instruction injection 的工程落地:编排器 LLM 根据上下文(profile/需求/task_type)选择注入哪些任务指令。

### 2.3 Mixture of Prompts(按 task type 混合)

[AAAI 2025 "Mixture of Prompts"](https://ojs.aaai.org/index.php/AAAI/article/view/33804/35959)(14 引用):

> 按 task type 选 prompt 组件混合,不同任务用不同 prompt 组合。

### 2.4 TAPO(动态选择 + 加权)

[arXiv:2501.06689](https://arxiv.org/html/2501.06689v1):

> 动态选择和加权 task-specific 的评估指标/prompt 组件。

---

## 3. 代码现状(评审者据此判断方案可行性)

> `src/story_lifecycle/` 简写为 `SL/`。行号基于 2026-07-15 代码。

### 3.1 _build_cli_prompt 的 section 拼接结构

`SL/orchestrator/engine/planner.py:1482-1632` 的 `_build_cli_prompt` 最终拼出这些 section:

```
## 任务: {stage}
### Story 信息           ← 静态(Key/标题/证据目录)
### 阶段说明             ← profile stage.description
### PRD / 需求详情       ← 只给文件路径(B 类按需)
{transcript_section}     ← 历史 session 摘要(A 类死包)
{knowledge_section}      ← kb.py 工具引导(B 类按需,所有 stage)
{dimensions_section}     ← 设计维度 checklist(A 类死包,**仅 design stage**)
{quality_section}        ← 质量检查清单(A 类死包,**仅 verify stage**)
### 关键要点             ← focus(planner 传入)
{worktree_section}       ← worktree 指令(A 类死包,有绑定时)
{exec_constraint_section} ← 执行约束(A 类死包,当前按 _is_single_stage 分支)
### 完成协议             ← done.json 格式(硬编码协议)
```

### 3.2 三类"按 stage 名硬编码"的片段

这三类正是本文档要改造的对象——从"按 stage 名"改成"按 LLM 选的动作清单":

#### (a) 设计维度 checklist — 仅 design

```python
# planner.py:1543-1547
dimensions_section = ""
if stage == "design":
    dimensions_section = build_design_dimensions_section(
        story_key, workspace, stage, interactive=interactive
    )
```

注入的是 13 维产品→技术转化框架 + 逐问澄清协议。

#### (b) 质量检查清单 — 仅 verify

```python
# planner.py:1527-1531
quality_section = ""
if stage == "verify":
    checklist = build_quality_section(story_key, stage)
    if checklist.strip():
        quality_section = f"\n{checklist}\n"
```

注入的是失败模式预防检查项 + open HIGH findings + "Run: pytest && ruff check"。

#### (c) 执行约束 — 按 stage 数分支(刚加的临时方案)

```python
# planner.py:1592-1608(临时硬编码,本文档要替换)
_is_single_stage = len(profile_stages) <= 1 if profile_stages else False
if _is_single_stage:
    exec_constraint_section = ("### 执行约束\n你是单阶段全干模式...\n"
        "**可以跑轻量自检**(pytest/ruff/tsc)...")
else:
    exec_constraint_section = ("### 执行约束（重要）\n"
        "**不要运行**耗时构建/测试命令...")
```

**问题**:这三个分支都是硬编码的,不看任务性质,新 profile/新 task_type 都要改 Python 代码加 if。

### 3.3 规划入口(run_orchestrator_agent)

`SL/orchestrator/engine/planner.py:188-328`(REFACTOR §5.4.1 改造后):

```python
class StagePlan(BaseModel):
    stage: str
    skip: bool = False
    focus: str = ""
    # task_actions: list[str] = []  ← 本文要加的

class PlanResult(BaseModel):
    stages: list[StagePlan]
```

LLM 返回每个 stage 的 skip/focus,转成 action dict 存 DB。执行时 `continue_orchestrator_agent` 读 action dict,调 `_build_cli_prompt`。

### 3.4 现有的中断/resume 基础设施(grill-me 复用)

grill-me 需要的"中断等人→resume"机制,代码库里已有:

| 机制 | 位置 | 用途 |
|---|---|---|
| `mcp__lifecycle__clarify` MCP 工具 | `orchestrator/mcp/clarify_server.py` | headless claude 通过 MCP 提问,人答经它返回 |
| interactive_pty 模式 | `planner.py:837` | claude 在终端直接问人 |
| stage_gate confirm 闸 | `planner.py:1277-1321` | stage 完成后 paused 等人确认推进 |
| done-gate resume | `planner.py:673-674` | resume 跳过已完成 stage,从未完成处续 |

**grill-me 本质**是让动作清单里的 interactive 动作(如 write_design_doc)触发"提问→中断→等人答→resume",复用上面 clarify + stage_gate 机制。不需要新建中断系统。

---

## 4. 方案:动作清单(task_actions)

### 4.1 预制动作库

新建 `SL/orchestrator/engine/task_actions.py`:

```python
TASK_ACTIONS = {
    "write_design_doc": {
        "desc": "调研现有代码，产出设计方案（数据流/接口/表结构/状态机）",
        "instruction": (
            "先调研现有代码结构和链路，产出设计方案。覆盖：数据流、接口契约、"
            "数据模型、核心逻辑、边界异常、安全。可参考 kb.py graph 查依赖关系。"
        ),
    },
    "write_code": {
        "desc": "按需求实现代码改动",
        "instruction": "按设计方案实现代码改动。改完确认语法/类型无误。",
    },
    "run_tests": {
        "desc": "运行测试确认改动正确",
        "instruction": "运行测试（pytest/ruff check）确认改动正确。测试失败就修，直到通过。",
    },
    "accept_review": {
        "desc": "自验收：对照需求逐条确认完成度",
        "instruction": "对照 PRD 需求逐条自验收。未完成的补上，确认所有需求点覆盖。",
    },
    "write_test_report": {
        "desc": "产出测试报告",
        "instruction": "产出测试报告：测了什么、结果、覆盖率。",
    },
    "write_delivery_doc": {
        "desc": "产出交付文档（变更摘要/影响面/回滚）",
        "instruction": "产出交付文档：变更摘要、影响面分析、回滚方案。",
    },
}
```

**设计原则**:
- 每个动作有 `desc`(给 LLM 看的简述,帮它选)和 `instruction`(给 CLI 看的执行指令)
- 动作库是编排层预制的,LLM **只选不编**——不能自己造新动作
- 新增动作只需加一行到 TASK_ACTIONS,不改 Python 逻辑

### 4.2 LLM 在规划时选动作

扩展 `StagePlan`:

```python
class StagePlan(BaseModel):
    stage: str
    skip: bool = False
    focus: str = ""
    task_actions: list[str] = []  # 新增：动作 key 列表
```

system prompt 里列出可选动作 + 描述:

```
## 可选任务动作（为每个 stage 选该做哪些）
- write_design_doc: 调研现有代码，产出设计方案
- write_code: 按需求实现代码改动
- run_tests: 运行测试确认改动正确
- accept_review: 自验收：对照需求逐条确认完成度
- write_test_report: 产出测试报告
- write_delivery_doc: 产出交付文档

根据需求性质和 profile 模式，为每个 stage 选合适的动作组合。
例如单阶段全干选全部；多阶段 design 只选 write_design_doc。
```

### 4.3 action dict 存 task_actions

```python
actions.append({
    "action": "launch", "adapter": ..., "stage": sp.stage,
    "focus": sp.focus,
    "task_actions": sp.task_actions,  # 新增
    "done_file": ...,
})
```

### 4.4 _build_cli_prompt 按 task_actions 组装

新增 `task_actions` 参数,组装"### 本阶段任务清单":

```python
def _build_cli_prompt(*, ..., task_actions=None):
    ...
    # 任务清单（替 stage 名隐含）
    task_list_section = _build_task_list(task_actions or [])
```

`_build_task_list` 实现:

```python
def _build_task_list(action_keys: list[str]) -> str:
    """把 LLM 选的动作 key 列表 → prompt 里的任务清单段。"""
    items = []
    for i, key in enumerate(action_keys, 1):
        action = TASK_ACTIONS.get(key)
        if action:
            items.append(f"{i}. {action['instruction']}")
    if not items:
        return ""
    return "\n### 本阶段任务清单\n请按以下顺序完成：\n" + "\n".join(items) + "\n"
```

prompt 最终结构(改动部分):

```
### 本阶段任务清单          ← 新(替 stage 名隐含该干什么)
请按以下顺序完成：
1. 先调研现有代码结构和链路，产出设计方案...
2. 按设计方案实现代码改动...
3. 运行测试确认改动正确...
...
{knowledge_section}         ← 不变
### 关键要点                 ← focus,不变
{exec_constraint_section}   ← 简化(见 4.5)
### 完成协议                 ← 不变
```

### 4.5 执行约束由 task_actions 隐含

**删掉 `_is_single_stage` 硬编码分支。** 执行约束简化为:

- task_actions 包含 `run_tests` → 允许跑轻量测试(pytest/ruff/tsc --noEmit)
- 不包含 → 只写代码,不需要跑测试
- 不管什么情况 → 都禁重构建(mvn/npm install/yarn install)

```python
def _build_exec_constraint(task_actions: list[str]) -> str:
    has_tests = "run_tests" in task_actions
    if has_tests:
        return ("### 执行约束\n可以跑轻量自检（pytest/ruff/tsc --noEmit）确认改动正确，"
                "但不要跑重构建（mvn/npm install/yarn install）。\n")
    return ("### 执行约束\n本阶段只写代码/文档，不需要跑测试。"
            "不要运行任何构建/测试命令。\n")
```

### 4.6 fallback(_default_planning_actions)

LLM 不可用时,按 stage 名给默认动作:

```python
_DEFAULT_TASK_ACTIONS = {
    "design":  ["write_design_doc"],
    "build":   ["write_code"],
    "verify":  ["run_tests", "accept_review", "write_test_report"],
}
# 单 stage profile(如 single-pass)→ 全干
_DEFAULT_SINGLE_STAGE_ACTIONS = [
    "write_design_doc", "write_code", "run_tests",
    "accept_review", "write_test_report",
]
```

---

## 5. grill-me(下一步方向,本文档只设计不实现)

### 5.1 问题

设计阶段不该无脑往下走。遇到关键岔路(多种方案/信息缺失/资方差异),该有 **grill-me 拉扯**:

```
CLI: "这个字段要不要历史版本？如果要，查询性能会受影响"
  ↓ 中断,等人答
人: "要，性能问题用缓存解"
  ↓ resume
CLI: (带答继续设计) "好，加 history 表 + Redis 缓存..."
  ↓ 可能再追问
CLI: "缓存过期策略用 TTL 还是主动失效？"
  ...
```

**效率更高的原因**:不拉扯的话,CLI 可能猜错方向(以为不要历史版本),写到一半才发现错了要返工。拉扯让关键决策前置,减少返工。

### 5.2 动作带 mode

每个动作可带 `mode`(下一步在 TASK_ACTIONS 加):

| mode | 行为 |
|---|---|
| `autonomous` | CLI 自己干完,不打断(write_code/run_tests/write_test_report) |
| `interactive` | 可提问/拉扯,等人介入后继续(write_design_doc/accept_review) |

### 5.3 复用现有中断/resume 机制

grill-me **不需要新建中断系统**。现有机制已经够用:

| grill-me 需要 | 复用的现有机制 | 位置 |
|---|---|---|
| CLI 提问 | `mcp__lifecycle__clarify` MCP 工具 | `orchestrator/mcp/clarify_server.py` |
| 中断等人 | clarify 工具天然阻塞(等人答才返回) | 同上 |
| 人答→resume | clarify 返回答案,CLI 带答继续 | 同上 |
| 终端直接问 | interactive_pty 模式 | `planner.py:837` |
| stage 级中断/恢复 | stage_gate confirm 闸 | `planner.py:1277-1321` |

**grill-me 的本质**:让动作清单里的 interactive 动作触发 clarify 提问。claude 在终端/headless 里已经有"遇关键岔路调 clarify"的 prompt 引导(见 `build_design_dimensions_section` 的逐问澄清协议),grill-me 是把它从"design stage 专属"推广到"任何 interactive 动作"。

### 5.4 grill-me 的边界

- 拉扯最多 N 轮(防止无限提问)
- 只有 interactive 动作才拉扯,autonomous 动作不打断
- 人可以随时说"别问了直接干"跳过拉扯

---

## 6. 改动文件清单

### 本次实现(动作清单)

| 文件 | 改动 |
|---|---|
| `engine/task_actions.py` | **新建**:TASK_ACTIONS 动作库 + `_build_task_list()` + `_build_exec_constraint()` |
| `engine/planner.py` | StagePlan 加 task_actions;system prompt 加动作说明;action dict 存 task_actions;`_build_cli_prompt` 加参数 + 组装任务清单;删 `_is_single_stage`;`_default_planning_actions` 加默认动作;调用点传 task_actions |
| `tests/test_task_actions.py` | **新建**:动作库完整性 + 组装 + fallback |

### 下一步(grill-me,本次不做)

| 文件 | 改动 |
|---|---|
| `engine/task_actions.py` | TASK_ACTIONS 每项加 `mode` 字段 |
| `engine/planner.py` | interactive 动作触发 clarify |
| `engine/prompt_sections.py` | 逐问澄清协议从 design 专属推广到 interactive 动作 |

---

## 7. 不改的部分

- **完成协议**(done.json 格式)——第②层握手协议,硬编码
- **工具引导**(kb.py)——固定工具集
- **Story 信息 / PRD / worktree**——静态信息
- **adapter 路由**——profile 护城河
- **接力拓扑**(per-stage PTY + done-gate)——第②层核心
- `build_design_dimensions_section` / `build_quality_section` 的内部逻辑——保留,调用方式从"按 stage 硬调"改成"task_actions 包含对应动作时才调"

---

## 8. 风险与开放问题

### 8.1 风险

| # | 风险 | 影响 | 缓解 |
|---|---|---|---|
| R1 | LLM 选错动作组合(如 design 不选 write_design_doc) | CLI 拿到错误任务清单 | fallback 兜底;动作 desc 写清楚帮 LLM 判断 |
| R2 | 动作清单和 done.json 的 expected_outputs 不一致 | 产出物对不上 | 下一步考虑 task_actions 驱动 expected_outputs |
| R3 | 动作库膨胀(加太多动作) | LLM 选择困难 | 保持精简(当前 6 个);arxiv 2601.04748 相变警告 |

### 8.2 开放问题(请评审者回应)

**Q1**:动作清单的粒度对不对?当前 6 个动作(write_design_doc/write_code/run_tests/accept_review/write_test_report/write_delivery_doc)会不会太粗或太细?

**Q2**:LLM 选动作时,system prompt 里该给多少指导?是只列动作 + desc(让 LLM 自由选),还是给"design 通常选 write_design_doc"之类的推荐组合?

**Q3**:task_actions 该不该和 profile 的 expected_outputs 联动?比如选了 write_test_report 就自动期望 test_report_path?

**Q4**:grill-me 的"最多 N 轮拉扯"该配在哪——profile(stage 级配)、还是动作级(每个 interactive 动作配)?

**Q5**:single-pass 单阶段全干时,如果 LLM 选了全部 6 个动作,prompt 会很长。要不要对单阶段做特殊处理(如分组)?

---

## 参考文献

1. [On Meta-Prompting (arXiv:2312.06562)](https://arxiv.org/html/2312.06562v4) — LLM 组装 sub-prompt + conditional instruction injection
2. [Mixture of Prompts (AAAI 2025)](https://ojs.aaai.org/index.php/AAAI/article/view/33804/35959) — 按 task type 选 prompt 组件混合
3. [TAPO (arXiv:2501.06689)](https://arxiv.org/html/2501.06689v1) — 动态选择 + 加权 task-specific prompt 组件
4. [ML Pills: Routing LLM Agent](https://mlpills.substack.com/p/diy-20-routing-llm-agent-with-langchain) — "one prompt fits all is a myth"
5. [REFACTOR-orchestrator-three-layer-positioning.md](./REFACTOR-orchestrator-three-layer-positioning.md) — 编排器三层定位(前置文档)
