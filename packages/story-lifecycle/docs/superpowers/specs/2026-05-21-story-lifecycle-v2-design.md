# Story Lifecycle Manager v2 — 设计文档

## 1. 产品定位

一个面向 AI 辅助开发的**需求生命周期管理工具**。将 TAPD 需求从 PRD 生成到上线发布的完整流程拆分为标准化阶段，通过 LangGraph 编排、Claude Code 执行、ttyd 终端交互。

**核心价值**：开发者只需要在终端里跟 Claude Code 对话和做关键决策，流程推进、异常恢复、多任务并行由工具自动管理。编排 LLM（DeepSeek）充当"规划协调器"，主动分析上下文、指挥执行 CLI、审查产出质量。

**开源定位**：`pip install story-lifecycle`，本地可用，远程可选。零数据库依赖（SQLite），零前端依赖（CLI + ttyd）。

## 2. 交互模型

### 2.1 CLI 管理 + ttyd 交互

```
┌─ 终端 1: story board ──────────────────────────────┐
│  STORY-1065520  brainstorming   ▶ active   终端: F2 │
│  STORY-1064120  backend_dev     ⏸ confirm 终端: F3 │
│  STORY-1063001  done           ✓ complete          │
│  STORY-1066000  prd_generator   ▶ active   终端: F4 │
└─────────────────────────────────────────────────────┘

按 F2 → 打开 tmux session s-1065520 → 看到 CC 正在运行
按 F3 → 打开 tmux session s-1064120 → CC 在等确认
       → 用户在终端里打 "确认，继续"
       → CC 继续执行
```

- CLI 负责管理操作：`story new`、`story board`、`story skip`
- ttyd 终端负责跟 CC 交互：确认决策、Review 输出、输入补充信息
- 不需要 Web UI
- 统一从 `story board` TUI 进入和管理（`[e]` 键进入终端）

### 2.2 命令清单

```bash
story new <TAPD-ID> --title "描述"          # 创建需求，开始 prd_generator
story board                                  # 看板：所有 story 进度汇总（TUI 交互式）
story status <key>                           # 查看单个 story 详情
story skip <key> --stage <name>              # 跳过指定阶段
story retry <key>                            # 重试当前阶段
story fail <key>                             # 标记失败
story archive <key>                          # 归档
story log <key>                              # 查看操作日志
```

## 3. 部署模式

### 3.0 CLI 模式切换（本地 vs 远程）

CLI 命令通过参数区分本地和远程模式，不用 fallback 逻辑：

```bash
# 本地模式（默认）— 直接调 service 层，不启动 server
story new STORY-001 --title "..."
story board
story enter STORY-001

# 远程模式 — 通过 --server 指定远端 orchestrator
story new STORY-001 --title "..." --server http://101:8180
story board --server http://101:8180
story enter STORY-001 --server http://101:8180
```

**设计原则**：
- 默认本地模式，零网络依赖
- `--server` 参数（或 `STORY_SERVER` 环境变量）切换到远程 HTTP 模式
- 两条路径完全独立，不存在"先试 HTTP 再 fallback"的逻辑
- 本地模式不需要 `story serve`，TUI 和 CLI 都直接操作本地 DB + service 层
- `story serve` 仅作为远端 API 服务器，供远程 CLI 连接

### 3.1 本地模式（默认）

```
开发机
├── story CLI              # pip install story-lifecycle
├── story-orchestrator     # 后台服务（LangGraph + FastAPI）
│   ├── story.db (SQLite)  # 业务数据 + checkpoint
│   ├── prompts/*.md       # 阶段 prompt 模板
│   └── story-stages.yaml  # 阶段定义
├── ttyd + tmux            # 终端服务
└── claude (CC)            # AI 执行引擎
```

`story` CLI 直接调本地 orchestrator API。

### 3.2 远程模式（团队共享）

```
开发机 A                   开发机 B
├── story CLI              ├── story CLI
│                          │
└────── API ───────────────┘
              │
团队服务器 (如 101)
├── story-orchestrator (:8180)
├── ttyd (:7681+, nginx → d.icebao.top)
├── CC + tmux
└── story.db
```

CLI 通过 `STORY_SERVER=http://101:8180` 环境变量切换到远程模式。ttyd 通过 nginx 反代暴露给开发者。

### 3.3 部署物

```
story-lifecycle/
├── pyproject.toml              # pip install
├── src/
│   ├── cli/
│   │   └── main.py             # Click CLI
│   ├── orchestrator/
│   │   ├── graph.py            # LangGraph StateGraph
│   │   ├── planner.py          # Smart Orchestrator（plan + review）
│   │   ├── router.py           # LLM 路由决策（unhappy-path 降级）
│   │   ├── nodes.py            # 图节点实现
│   │   ├── service.py          # 共享 service 层
│   │   ├── api.py              # FastAPI server
│   │   └── models.py           # SQLite models
│   └── terminal/
│       └── ttyd.py             # ttyd/tmux 管理
├── prompts/                    # 阶段 prompt 模板
│   ├── prd_generator.md
│   ├── brainstorming.md
│   ├── backend_dev.md
│   └── ...
└── story-stages.yaml           # 阶段定义
```

## 4. 目录结构：手脚在项目，大脑在用户

### 4.1 核心原则

参照 Claude Code（`.claude/`）、Aider（`.aider*`）的设计，AI 工具的数据有两种截然不同的生命周期：

| 数据类型 | 生命周期 | 放哪里 | 例子 |
|----------|----------|--------|------|
| **临时产物** | 跟项目走，随 stage 生灭 | 项目目录 | CC 写入的 `.done` JSON、生成的 spec 文档 |
| **持久状态** | 跟用户走，跨项目跨会话 | 用户目录 | 数据库、checkpoint、profiles、prompts |

**核心逻辑**：Claude Code 是单项目辅助工具，所以全放 `.claude/`。但 Story Lifecycle 是**多项目调度器**——`story board` 需要跨项目汇总。数据库必须在用户级。

### 4.2 推荐布局

```
# ── 用户空间：全局调度大脑 ──
~/.story-lifecycle/
├── story.db                    # SQLite（跨项目看板的数据源）
├── checkpoint.db               # LangGraph 状态持久化
├── profiles/                   # 全局阶段模板
│   ├── minimal.yaml
│   └── my-team.yaml
└── prompts/                    # 全局 Prompt 模板
    └── implement.md

# ── 项目空间：手脚临时产物 ──
~/hc-all/
├── .story-done/                # CC 产出（读完即删，.gitignore）
│   └── STORY-001/
│       └── design.json
├── .story/                     # 项目级配置（可选，覆盖全局）
│   └── config.yaml             # 如：这个项目强制用 standard profile
├── .gitignore                  # 追加 .story-done/ .story/
├── src/main/java/...
└── docs/                       # AI 产出的设计文档（需 git 追踪）

~/baoxian/
├── .story-done/
│   └── STORY-002/
│       └── implement.json
└── ...
```

### 4.3 数据分离逻辑

```python
# CC 写入临时产物 → 项目目录
done_file = Path(workspace) / ".story-done" / story_key / f"{stage}.json"

# Orchestrator 读取 → 也在项目目录
with file_lock(done_file):
    data = robust_json_parse(done_file)
done_file.unlink()  # 读完即删

# 数据库和 checkpoint → 用户目录
db_path = Path.home() / ".story-lifecycle" / "story.db"
checkpoint_path = Path.home() / ".story-lifecycle" / "checkpoint.db"
```

### 4.4 为什么不能用项目级 DB

如果 `story.db` 放在项目里：

```bash
$ cd ~/hc-all && story board      # 只看到 hc-all 的 story
$ cd ~/baoxian && story board     # 只看到 baoxian 的 story
# 无法跨项目汇总 →
```

必须是用户级 DB 才能实现：

```bash
$ story board                     # 无论在哪个目录，看到全部 story
┌──────────┬──────────┬──────────────┬─────────────┐
│ Story    │ Stage    │ Status       │ Workspace   │
├──────────┼──────────┼──────────────┼─────────────┤
│ STORY-001│ design   │ ▶ active     │ ~/hc-all    │
│ STORY-002│ implement│ ⏸ confirm    │ ~/baoxian   │
└──────────┴──────────┴──────────────┴─────────────┘
```

### 4.5 项目内嵌模式（可选）

通过 `STORY_HOME=.story` 切回单项目模式（适合 CI/CD 或单项目开发者）：

```bash
$ cd ~/hc-all
$ export STORY_HOME=.story
$ story new STORY-001       # ~/hc-all/.story/story.db
$ story board               # 只看当前项目
```

### 4.6 远程模式

```bash
export STORY_HOME=/srv/story-lifecycle   # 团队服务器共享目录
export STORY_SERVER=http://101:8180      # Orchestrator API
# .story-done/ 仍在各项目的 workspace 下，但 workspace 在服务器上
```

## 5. 数据模型

### 5.1 SQLite 表结构

```sql
CREATE TABLE story (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    story_key TEXT NOT NULL UNIQUE,        -- STORY-1065520
    title TEXT,
    workspace TEXT NOT NULL,               -- 项目路径，如 /home/ubuntu/hc-all
    current_stage TEXT NOT NULL,           -- brainstorming
    status TEXT NOT NULL DEFAULT 'active', -- active/paused/blocked/completed/archived
    complexity TEXT,                       -- S/M/L
    context_json TEXT DEFAULT '{}',        -- {prd_path, spec_path, affected_services, ...}
    execution_count INTEGER DEFAULT 0,
    last_error TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE stage_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    story_id INTEGER REFERENCES story(id),
    stage TEXT NOT NULL,
    action TEXT NOT NULL,      -- enter/complete/skip/retry/fail
    detail TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE gate_result (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    story_id INTEGER REFERENCES story(id),
    stage TEXT NOT NULL,
    gate_name TEXT NOT NULL,
    result TEXT NOT NULL,      -- pass/fail/skip
    detail TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
```

### 5.2 LangGraph Checkpoint

LangGraph 使用内置 `SqliteSaver`，checkpoint 表由 LangGraph 自动管理，与业务数据在同一个 SQLite 文件中。

## 6. 多 CLI 支持

工具不绑定 Claude Code。每个阶段通过 `cli` 字段指定执行引擎，支持：

| CLI | 启动命令 | provider 切换 |
|-----|---------|--------------|
| `claude` | `claude --model {model}` | `cc use {provider}` |
| `codex` | `codex exec --model {model}` | 内置 model 参数 |
| `gemini` | `gemini --model {model}` | 内置 model 参数 |
| `qoder` | `qodercli` | `/model` 交互切换（阿里云 Model Studio） |
| `aider` | `aider --model {model}` | 启动参数指定 |

**实现方式**：`cli` 字段映射到一个 adapter，adapter 封装 provider 切换和启动命令的差异。

**Smart Orchestrator 集成**：编排 LLM 在 `plan_stage` 中决定使用哪个 adapter。如果编排 LLM 可用，它会根据上下文和任务特征选择最合适的 CLI 工具、provider 和 model。如果不可用，从 profile 配置中读取默认值。

```python
# adapters/claude.py
class ClaudeAdapter:
    def switch_provider(self, provider): return f"cc use {provider}"
    def launch_cmd(self, model):         return f"claude --model {model}"
    def inject_prompt(self, prompt):     return f"cat prompt.md | claude -p -"

# adapters/codex.py
class CodexAdapter:
    def switch_provider(self, provider): return None  # Codex 用原生 Gemini，不用切
    def launch_cmd(self, model):         return f"codex exec --model {model}"
    def inject_prompt(self, prompt):     return f"codex exec --model {model} --prompt-file prompt.md"

# adapters/aider.py
class AiderAdapter:
    def switch_provider(self, provider): return f"export AIDER_MODEL={provider}"
    def inject_prompt(self, prompt, story_key, stage):
        # Aider 不支持 stdin，用 --message-file 代替
        # 文件名含 stage，防止重试时读到旧 prompt
        prompt_file = f"/tmp/story-{story_key}-{stage}.md"
        Path(prompt_file).write_text(prompt)
        return f"--message-file {prompt_file}"
    def cleanup(self, story_key, stage):
        # 阶段完成后清理临时 prompt 文件
        Path(f"/tmp/story-{story_key}-{stage}.md").unlink(missing_ok=True)
    def launch_cmd(self, model, prompt_arg):
        return f"aider --model {model} {prompt_arg}"
```

## 7. 阶段配置：Profile 系统

不再只有一套 14 阶段。工具内置 3 级 profile，用户可以自定义。

### 6.1 minimal（3 阶段，默认）

```yaml
# profiles/minimal.yaml
version: 2
cli: claude                          # 默认 CLI
stages:
  design:
    order: 1
    description: "需求分析与方案设计"
    confirm: false
    expected_outputs: [spec_path]
    next_default: [implement]

  implement:
    order: 2
    description: "编码实现"
    confirm: false
    expected_outputs: []
    next_default: [test]

  test:
    order: 3
    description: "编译验证与冒烟测试"
    confirm: false
    expected_outputs: []
    next_default: []
```

### 6.2 standard（14 阶段，即当前 v1 完整流程）

```yaml
# profiles/standard.yaml
version: 2
cli: claude
stages:
  prd_generator:
    order: 1
    description: "从 TAPD 生成结构化 PRD"
    skill: "/prd-generator"
    expected_outputs: [prd_path]
    next_default: [brainstorming]

  brainstorming:
    order: 2
    description: "需求分析，确定复杂度和方案"
    skill: "/brainstorming"
    expected_outputs: [complexity, affected_services]
    next_default:
      S: []
      M: [prepare_branches]
      L: [prepare_branches]

  prepare_branches:      # order 3
  writing_plans:         # order 4
  prd_review:            # order 5
  orchestrate:           # order 6
  backend_dev:           # order 7
  frontend_dev:          # order 8
  db_migration:          # order 9
  build_check:           # order 10
  deploy_test:           # order 11
  test_runner:           # order 12
  test_report:           # order 13
  production:            # order 14
```

### 6.3 用户自定义

```bash
# 用 minimal 开始
$ story new STORY-1065520 --title "..." --profile minimal

# 用 standard 开始（适合大需求）
$ story new STORY-1065520 --title "..." --profile standard

# 用户自定义 profile
$ cat > ~/.story-lifecycle/profiles/my-team.yaml << EOF
cli: claude
stages:
  spec:
    order: 1
    description: "编写技术方案"
    expected_outputs: [spec_path]
    next_default: [code_review]
  code_review:
    order: 2
    description: "AI code review"
    cli: codex                        # 这个阶段用 Codex
    expected_outputs: [review_result]
    next_default: [implement]
  implement:
    order: 3
    description: "编码实现"
    cli: claude                       # 回到 Claude
    expected_outputs: []
    next_default: []
EOF

$ story new STORY-9999999 --profile my-team
```

### 6.4 每个阶段可独立指定 CLI

```yaml
stages:
  code_review:
    cli: codex          # code review 天生适合 Gemini/Codex
    model: gemini-3-pro
    
  implement:
    cli: claude         # 编码用 Claude
    provider: deepseek
    model: sonnet
```

### 6.5 YAML 完整字段

```yaml
stages:
  <stage_name>:
    order: <int>                        # 顺序编号
    description: <str>                  # 阶段说明
    cli: <claude|codex|gemini|aider>    # CLI 引擎（继承 profile 默认值）
    provider: <str>                     # AI provider（引擎相关）
    model: <str>                        # 模型名
    skill: <str>                        # CC skill 调用（可选）
    confirm: <bool>                     # 暂停等人确认（默认 false）
    max_retries: <int>                  # 最大重试次数（默认 2）
    allowed_providers: [<str>]          # 重试时可切换的 provider 列表
    expected_outputs: [<str>]           # 必须产出的 context 字段
    next_default:                       # happy path 路由
      default: [<stage>]                #   → 固定下一阶段
      S: [] M: [...] L: [...]          #   → 按复杂度分支（可选）
    prompts:                            # prompt 生成（可选）
      template: prompts/<stage>.md      #   → 模板文件
      context_vars: [title, story_key]  #   → 注入的 context 变量
```

### 5.3 吸取 v1 已验证特性

| v1 特性 | v2 对应 |
|---------|---------|
| `executeStage`: provider 切换 → prompt 渲染 → tmux 启动 CC → prompt 注入 | `execute_stage_node`（Python subprocess，CLI adapter 抽象） |
| `TerminalService.ensureTtyd`: 端口分配 + ttyd 进程管理 | `ttyd.py` 模块（进程管理 + 端口池） |
| `PromptService.render`: 从 `prompts/*.md` 加载 + 变量替换 | `string.Template` + `context` dict |
| `StageConfigLoader`: 解析 YAML | `yaml.safe_load()`，支持多 profile |
| `ClaudeCodeService.polling`: 轮询 | `poll_completion_node`（LangGraph 原生） |
| `StoryService.advance`: gate 检查 + next 路由 | `router_node` + `advance_node`（LLM 动态路由） |
| — | `plan_stage_node` + `review_stage_node`（Smart Orchestrator 规划 + 审查） |

### 5.4 StateGraph 节点设计

```python
class StoryState(TypedDict):
    story_key: str
    title: str
    current_stage: str
    status: str                           # active/paused/blocked/completed
    complexity: Optional[str]
    context: dict
    execution_count: int
    last_error: Optional[str]
    provider_override: Optional[str]      # LLM 可覆盖 provider
    action: str                           # continue/retry/skip/fail/wait_confirm
    mode: str                             # local/remote
    plan: Optional[dict]                  # plan_stage 输出（Smart Orchestrator）
    review: Optional[dict]                # review_stage 输出（Smart Orchestrator）

graph = StateGraph(StoryState)

# 节点
graph.add_node("plan_stage", plan_stage_node)         # Smart Orchestrator 规划
graph.add_node("execute_stage", execute_stage_node)
graph.add_node("poll_completion", poll_completion_node)
graph.add_node("review_stage", review_stage_node)     # Smart Orchestrator 审查
graph.add_node("router", router_node)
graph.add_node("advance", advance_node)
graph.add_node("retry", retry_node)
graph.add_node("skip_stage", skip_node)
graph.add_node("fail_stage", fail_node)
graph.add_node("wait_confirm", wait_confirm_node)

# 边
graph.add_edge(START, "plan_stage")                    # 先规划
graph.add_edge("plan_stage", "execute_stage")          # 再执行
graph.add_edge("execute_stage", "poll_completion")
graph.add_edge("poll_completion", "review_stage")      # poll 后审查
graph.add_edge("review_stage", "router")               # 审查后路由
graph.add_conditional_edges(
    "router",
    router_node,                          # 调用 LLM 决策
    {
        "advance": "advance",
        "retry": "retry",
        "skip": "skip_stage",
        "fail": "fail_stage",
        "wait_confirm": "wait_confirm",
    }
)
graph.add_edge("advance", "plan_stage")   # 推进后重新规划
graph.add_edge("retry", "plan_stage")     # 重试后重新规划
graph.add_edge("skip_stage", "advance")   # 跳过当前 → 推进
graph.add_edge("fail_stage", END)         # 阻塞，等人介入
graph.add_edge("wait_confirm", "plan_stage") # 确认后重新规划
```

**Graph 流程**：
```
START → plan_stage → execute_stage → poll_completion → review_stage → router → advance/retry/skip/fail/wait_confirm
              ↑                                                             │
              └──────────────── retry (with feedback) ──────────────────────┘
```

### 5.5 Smart Orchestrator（规划协调器）

编排 LLM（DeepSeek）充当"规划协调器"，不做执行，只读状态、做决策、给执行 CLI 下指令。

**设计原则**：
- 编排器不写代码，只指挥
- 无 API Key 时退化为当前行为（从 profile 配置生成默认 plan）
- 渐进式增强：初期 review 只做日志记录，稳定后再开启自动 retry

#### plan_stage 节点

在 `execute_stage` 之前运行。编排 LLM 分析上下文，决定执行方案：

```python
def plan_stage_node(state: StoryState) -> StoryState:
    """编排 LLM 规划当前阶段。无 LLM 时退化为默认 plan。"""
    if planner.is_available():
        plan = planner.plan_stage(state, stage_config, adapters)
        state["plan"] = plan
        if plan.get("skip"):
            return skip_node(state)
    else:
        # 退化：用 profile 配置生成 plan
        state["plan"] = {
            "adapter": cfg.get("cli", "claude"),
            "provider": cfg.get("provider", "deepseek"),
            "model": cfg.get("model", "sonnet"),
            "skip": False,
            "extra_instructions": "",
        }
    return state
```

Plan 输出结构：
```json
{
  "adapter": "claude",
  "provider": "deepseek",
  "model": "sonnet",
  "skip": false,
  "extra_instructions": "根据 docs/design.md 中的方案实现用户认证模块...",
  "reasoning": "设计阶段已完成，spec 明确，直接进入实现"
}
```

`extra_instructions` 是给执行 CLI 的具体指引——编排器指挥执行 CLI 做什么。

#### review_stage 节点

在 `poll_completion` 之后运行。编排 LLM 审查产出质量：

```python
def review_stage_node(state: StoryState) -> StoryState:
    """编排 LLM 审查阶段产出。无 LLM 时跳过审查。"""
    if planner.is_available():
        review = planner.review_stage(state, stage_config, stage_output)
        state["review"] = review
        if review.get("quality") == "revise":
            state["last_error"] = f"Review feedback: {review.get('feedback')}"
        elif review.get("quality") == "fail":
            state["last_error"] = f"Review failed: {review.get('feedback')}"
    return state
```

Review 输出结构：
```json
{
  "quality": "pass|revise|fail",
  "feedback": "审查意见",
  "context_updates": {},
  "reasoning": "判断理由"
}
```

#### 退化策略

| 条件 | 行为 |
|------|------|
| `STORY_LLM_API_KEY` 未设置 | plan/review 跳过 LLM，用 profile 配置 |
| LLM 调用超时/失败 | 降级为默认 plan / 跳过 review |
| plan JSON 解析失败 | 降级为默认 plan |
| review JSON 解析失败 | 跳过审查，直接 advance |

详见 `docs/design-smart-orchestrator.md`。

### 5.6 路由决策

```python
def router_node(state: StoryState) -> str:
    """决定下一步：advance/retry/skip/fail/wait_confirm"""

    # review 阶段标记了 revise → 带反馈重试
    review = state.get("review")
    if review and review.get("quality") == "revise":
        return "retry"
    if review and review.get("quality") == "fail":
        return "fail"

    # Happy path：CC 正常完成，无错误
    if state.get("last_error") is None and state["status"] == "active":
        stage_cfg = get_stage_config(state["current_stage"])
        if stage_cfg.get("confirm"):
            return "wait_confirm"
        return "advance"

    # Unhappy path：错误/超时/异常 → LLM 决策
    prompt = f"""当前需求 {state['story_key']} 在阶段 {state['current_stage']} 遇到问题。

错误信息: {state.get('last_error', '无')}
已重试: {state['execution_count']} 次
阶段最大重试: {stage_cfg.get('max_retries', 1)} 次
当前上下文: {json.dumps(state['context'], ensure_ascii=False)}

请决定下一步：
- retry: 重试当前阶段（可切换 provider）
- skip: 跳过此阶段（需理由充分）
- fail: 标记失败，等待人工介入

返回 JSON: {{"action": "retry|skip|fail", "reasoning": "...", "provider": "..."}}"""

    result = llm.invoke(prompt)
    return parse_action(result)
```

### 5.6 Prompt 模板解耦

v1 的问题：prompt 模板里写了 curl 命令，CC 负责调 API 推进流程。

v2：CC 只需产出，推进由 orchestrator 负责。Smart Orchestrator 在 `plan_stage` 生成的 `extra_instructions` 会注入到 prompt 前面，作为"优先指引"。

```markdown
# brainstorming.md (v2)

调用 /brainstorming skill 进行需求分析。

## 背景

- Story Key: {story_key}
- 标题: {title}
- PRD 路径: {prd_path}

## 完成后

将分析结果写入**项目根目录**下的 `.story-done/{story_key}/brainstorming.json`：

```json
{
  "complexity": "S|M|L",
  "affected_services": ["hc-user"],
  "summary": "简要分析摘要"
}
```

> 注意：写入 `.story-done/{story_key}/` 目录（按 story 分子目录）。
> 不要写绝对路径，直接写 `.story-done/{story_key}/brainstorming.json` 即可。

系统会自动检测该文件并推进到下一阶段。
```

Orchestrator 的 `poll_completion_node` 从 workspace 拼接路径读取：

```python
done_file = Path(state["workspace"]) / ".story-done" / state["story_key"] / f"{state['current_stage']}.json"
```

## 8. 关键工程细节

### 7.1 .done JSON 容错解析（核心）

大模型输出 JSON 极其不可控——经常在前后加 markdown 代码块或废话：

```
好的，这是分析结果：
```json
{"complexity": "M", ...}
```
希望对你有所帮助！
```

`poll_completion_node` 必须做容错解析，不能直接 `json.loads()`：

```python
def robust_json_parse(filepath: str) -> dict:
    raw = filepath.read_text()
    
    # 策略1: 尝试直接解析
    try:
        return json.loads(raw)
    except JSONDecodeError:
        pass
    
    # 策略2: 正则提取第一个 {...} 对象
    m = re.search(r'\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}', raw, re.DOTALL)
    if m:
        try:
            return json.loads(m.group())
        except JSONDecodeError:
            pass
    
    # 策略3: 轻量 LLM 提取（最后防线）
    return tiny_llm_extract(raw)
```

同时 prompt 模板中必须用极强语气约束输出格式：

```markdown
## 完成后

将结果写入 `.done/brainstorming.json`。

**CRITICAL**: The file must contain ONLY raw JSON. NO markdown blocks, NO
explanations. If you output anything other than pure JSON, the system
will fail to parse it and the stage will be treated as failed.
```

### 7.2 expected_outputs 校验闭环（Schema Guard）

YAML 中定义了 `expected_outputs`，但必须在 `advance_node` 中强制校验：

```python
def advance_node(state: StoryState) -> StoryState:
    stage_cfg = get_stage_config(state["current_stage"])
    expected = stage_cfg.get("expected_outputs", [])
    
    missing = [k for k in expected if k not in state.get("context", {})]
    if missing:
        state["last_error"] = f"Missing expected outputs: {missing}"
        return state  # 走 Unhappy path → router_node 决策重试/跳过
    
    # 校验通过，推进到下一阶段
    next_stage = resolve_next_stage(state)
    state["current_stage"] = next_stage
    return state
```

### 8.3 Polling 策略（非阻塞）

**关键约束**：LangGraph 节点不能长时间阻塞。如果一个节点 `while True: sleep(30)` 等待 CC 完成，会死死占用工作线程，导致其他 story 全部卡死。

**Phase 1 方案：线程隔离**。每个 story 的 LangGraph thread 在独立线程中运行，`time.sleep()` 只阻塞该 story 自己的工作线程，不影响其他 story。

```python
executor = ThreadPoolExecutor(max_workers=max_concurrent)

def run_story(story_key: str):
    """每个 story 在独立线程中执行，阻塞不影响其他 story"""
    graph.invoke(initial_state, config={"configurable": {"thread_id": story_key}})

# 启动 story
executor.submit(run_story, "STORY-001")
```

**启动恢复**：Orchestrator 重启后，内存中的 ThreadPool 会丢失。必须在 FastAPI 启动时扫描 `active` 状态的 story 重新提交：

```python
@app.on_event("startup")
def recover_orphan_stories():
    active = db.query("SELECT story_key FROM story WHERE status = 'active'")
    for row in active:
        executor.submit(run_story, row.story_key)
    log.info(f"Recovered {len(active)} active stories after restart")
```

这样服务器重启或代码热更后，正在运行的 story 不会变成僵尸。

**Phase 2 方案：LangGraph interrupt + Watchdog**。更优的做法是让 Graph 主动让出控制权：

```python
def execute_stage_node(state):
    launch_cc(state)  # 启动 CC
    return state      # 不进入 poll，直接结束

def poll_completion_node(state):
    # 这个节点由 Watchdog 触发，不是在 Graph 内死循环
    done_file = Path(STORY_HOME) / ".done" / state["story_key"] / f"{state['current_stage']}.json"
    if done_file.exists():
        state["context"].update(robust_json_parse(done_file))
        done_file.unlink()
        return state
    if not tmux_session_alive(f"s-{state['story_key']}"):
        state["last_error"] = "CC crashed"
        return state
    # 还没完成 → 等一下再检查
    import langgraph
    raise langgraph.GraphRecursionError("Not ready, retry later")
```

Watchdog 定时扫描 `.done/` 文件，发现完成后通过 `graph.ainvoke(None, thread_config)` 唤醒 graph 继续执行。

Phase 1 先用线程隔离方案（简单可靠），Phase 2 再切换到 interrupt + Watchdog。

### 8.4 跳过阶段的上下文补全（Skip Guard）

`expected_outputs` 的 Schema Guard 在跳过阶段时有逻辑漏洞。假设 `frontend_dev` 要求产出 `frontend_files`，LLM 决定跳过它（因为无前端变更）。下一阶段的 Schema Guard 会发现 `frontend_files` 缺失而报错。

**解决**：Skip 节点必须为被跳过阶段的 `expected_outputs` 自动填充占位值：

```python
def skip_node(state: StoryState) -> StoryState:
    stage_cfg = get_stage_config(state["current_stage"])
    expected = stage_cfg.get("expected_outputs", [])
    
    for key in expected:
        if key not in state.get("context", {}):
            state["context"][key] = "SKIPPED"
    
    log_stage(state["story_key"], state["current_stage"], "skip")
    return state
```

后续阶段读到 `"SKIPPED"` 就知道该阶段被跳过，不会误判为缺失。

### 8.5 进程崩溃检测（双通道 Poll）

`poll_completion_node` 不能只等 `.done` 文件——如果 CC 进程崩溃，文件永远不会生成。

```python
def check_stage_ready(state: StoryState) -> bool:
    done_dir = Path(state["workspace"]) / ".story-done" / state["story_key"]
    done_file = done_dir / f"{state['current_stage']}.json"
    session = f"s-{state['story_key']}"
    
    # 通道1: .done 文件生成 → 成功
    if done_file.exists():
        with file_lock(done_file):           # 文件锁防读脏数据
            state["context"].update(robust_json_parse(done_file))
        done_file.unlink()                   # 清理，防止重试误判
        return True
    
    # 通道2: tmux 进程存活检测
    if not tmux_session_alive(session):
        state["last_error"] = "CC process crashed (tmux session dead)"
        return True  # 也返回 True，但带有 last_error → 进入 router
    
    # 通道3: 超时检测
    if time.time() - state.get("stage_start_time", 0) > TIMEOUT_SECONDS:
        state["last_error"] = f"Stage timeout after {TIMEOUT_SECONDS}s"
        return True
    
    return False  # 还没准备好，继续等

### 8.6 并发限流（Worker Pool）

同时跑 N 个 story 可能打爆 API 配额或机器资源：

```python
class Orchestrator:
    def __init__(self, max_concurrent: int = 2):
        self.semaphore = asyncio.Semaphore(max_concurrent)
    
    async def execute_stage_node(self, state: StoryState):
        async with self.semaphore:
            # 超出上限的 story 自动排队等待
            return await self._do_execute(state)
```

环境变量 `STORY_MAX_CONCURRENT=2` 控制，默认 2。

### 8.7 Source of Truth（状态一致性）

LangGraph checkpoint 和 SQLite story 表都存了状态，存在双写不一致风险：

```
方案（推荐）：LangGraph checkpoint = 唯一 Source of Truth

SQLite story 表 = 只读视图，方便 CLI 列表查询
每次 Graph 状态变更 → 异步更新 SQLite（更新失败不影响核心流程）
```

CLI `story board` 读 SQLite（快速列表查询）。`story enter` / `story status` 读 LangGraph checkpoint（实时详情）。

### 8.8 tmux CWD 与会话管理

**工作目录**：tmux session 创建时必须指定 `-c` 参数，否则默认 CWD 是 Orchestrator 的启动目录。CC/Aider 会在错误的目录下读写代码：

```python
def create_tmux_session(session_name: str, workspace: str):
    subprocess.run([
        "tmux", "new-session", "-d",
        "-s", session_name,
        "-c", workspace            # 关键：指定工作目录
    ])
```

或者进入 session 后先发 `cd {workspace} && clear` 再启动 CLI。

**僵尸会话清理**：Orchestrator 启动时扫描并清理无主 tmux 会话：

```python
def cleanup_orphaned_sessions():
    """清理没有对应 active story 的 tmux 会话"""
    active_sessions = {f"s-{s.story_key}" for s in get_active_stories()}
    for tmux_session in list_tmux_sessions():
        if tmux_session.startswith("s-") and tmux_session not in active_sessions:
            subprocess.run(["tmux", "kill-session", "-t", tmux_session])
```

### 8.9 CLI 渲染（rich 库）

`story board` 使用 `rich` 库实现格式化和交互：

```python
from rich.table import Table
from rich.console import Console
from rich.live import Live

# 彩色状态标签、自动刷新、键盘快捷键
table = Table(title="Story Board")
table.add_column("Story", style="cyan")
table.add_column("Stage", style="green")
table.add_column("Status")
...
```

### 8.10 远程模式安全（Phase 2 实现，Phase 1 只记录）

- ttyd 远程访问必须经 JWT 鉴权（Nginx 层或 FastAPI middleware）
- 多人同时访问同一 story 终端需提示冲突
- `wait_confirm` 阶段通过钉钉/飞书/Slack webhook 推送通知
- Prompt 注入防御：router_node 输入做白名单过滤，限制 LLM 只能选择预定义 action

## 9. 多任务并行

### 8.1 实现方式

```
story.db
├── STORY-1065520  → LangGraph thread-1065520  → tmux s-1065520  → ttyd :7701
├── STORY-1064120  → LangGraph thread-1064120  → tmux s-1064120  → ttyd :7702
├── STORY-1063001  → LangGraph thread-1063001  (已完成)
└── STORY-1066000  → LangGraph thread-1066000  → tmux s-1066000  → ttyd :7703
```

每个 story 是 LangGraph 的一个 thread（独立 checkpoint），拥有自己的 tmux session 和 ttyd 端口。Orchestrator 并发执行多个 thread。

### 8.2 story board 视图

```bash
$ story board
┌──────────────────┬────────────────┬─────────┬───────┬────────┬──────────────┐
│ Story            │ Stage          │ Status  │ Compl │ Retry  │ Terminal     │
├──────────────────┼────────────────┼─────────┼───────┼────────┼──────────────┤
│ STORY-1065520    │ brainstorming  │ ▶ active│ M     │ 0      │ F2 → :7701   │
│ STORY-1064120    │ backend_dev    │ ⏸ conf │ M     │ 1      │ F3 → :7702   │
│ STORY-1066000    │ prd_generator  │ ▶ active│ -     │ 0      │ F4 → :7704   │
│ STORY-1063001    │ done           │ ✓ comp │ S     │ 0      │ -            │
└──────────────────┴────────────────┴─────────┴───────┴────────┴──────────────┘

Commands: [N]ew  [E]nter  [S]kip  [R]etry  [F]ail  [A]rchive  [Q]uit
```

## 10. 分阶段实现

### Phase 1：单任务闭环 + minimal profile

- SQLite 数据模型
- FastAPI CRUD 端点
- `profiles/minimal.yaml`（3 阶段：design → implement → test）
- Claude adapter（CLI 抽象层）
- LangGraph 单 graph（plan → execute → poll → review → router → advance），**不含 LLM Router，先全用 if-else**
- Smart Orchestrator 基础（planner.py，plan + review 节点，退化策略）
- `.done` JSON 容错解析（防 markdown 包裹）
- `expected_outputs` Schema Guard 校验
- 双通道 poll（`.done` 文件 + tmux 进程存活）
- 文件锁 + 僵尸会话清理
- ttyd/tmux 管理
- CLI（`story new/board/skip`）使用 `rich` 渲染
- `story board` TUI 交互看板
- **验证**：STORY-1065520 在 minimal profile 下完整跑通

### Phase 2：standard profile + 多 CLI + 异常恢复

- `profiles/standard.yaml`（14 阶段完整流程）
- Codex / Gemini / Aider adapter
- Profile 选择（`--profile standard`）
- LangGraph 多 thread 并发
- LLM router（异常决策 + provider 切换）
- Smart Orchestrator 增强（quality gate 自动 retry，跨阶段 context 传递优化）
- 远程模式支持
- **验证**：3 个 story 用不同 profile 和 CLI 同时运行，异常场景自动恢复

### Phase 3：自定义 profile + 开源就绪

- 用户自定义 profile 加载（`~/.story-lifecycle/profiles/`）
- `pyproject.toml` + PyPI 发布
- 配置文件化（`~/.story-lifecycle/config.yaml`）
- 文档 + README + 示例项目
- **验证**：`pip install story-lifecycle` → `story new --profile my-team` → 完整流程

## 11. 风险与决策

| 决策点 | 选择 | 理由 |
|--------|------|------|
| ORM | 不用 SQLAlchemy，用 raw sqlite3 | 3 张表不需要 ORM |
| Web 框架 | FastAPI | LangGraph 原生集成，uvicorn 部署简单 |
| CLI 框架 | Click（+ rich 渲染） | 成熟稳定，rich 表格/颜色原生支持 |
| 远程通信 | HTTP (httpx) | 不引入 gRPC/Redis 等重型依赖 |
| ttyd 安装 | 外部依赖（apt/brew） | 不打包进 pip，用户自行安装 |
| tmux | 外部依赖 | macOS/Linux 自带或可装 |
| JSON 解析 | 容错解析（正则 + 可选轻量 LLM） | 大模型输出不可控，不能直接 json.loads |
| 并发控制 | asyncio.Semaphore，默认 max=2 | 防 API 配额打爆 |
| 状态一致性 | LangGraph checkpoint 为主，SQLite 为只读视图 | 避免双写不一致 |
| 远程鉴权 | Phase 2 实现，Phase 1 只记录 | JWT + nginx，远程模式暂不开发 |
| Polling 策略 | Phase1: 线程隔离; Phase2: LangGraph interrupt + Watchdog | 避免阻塞其他 story |
| Smart Orchestrator | plan_stage + review_stage，每阶段 2 次 LLM 调用 | 用 fast model + low tokens，约 2-3s/次；无 API Key 时退化 |
| Skip 上下文 | skip_node 自动填充 SKIPPED | 防止跳过阶段后 Schema Guard 误报 |
| Profile 安全 | `yaml.safe_load()` 兜底，社区 profile 限制 cli 枚举值 | 防止 YAML RCE |
| YAML 解析 | `yaml.safe_load()`（不用 `yaml.load()`） | 禁止 `!!python/object` 等危险标签 |
| Aider adapter | `--message-file` 替代 stdin | Aider 不支持管道输入 |

## 12. 开源定位

### 11.1 市场定位

当前 AI 编码工具格局：

| 工具 | 定位 | 局限 |
|------|------|------|
| Aider | AI 编码助手 | 单模型、单会话、无多阶段编排 |
| Continue.dev | IDE 插件 | 无流程管理、无多任务并行 |
| Cline | VS Code 对话式编程 | 同上 |
| Dify/Coze | 工作流编排 | 非 CLI 原生、非编码场景 |
| OpenHands | Web 终端 + 文件树 | 单任务、重 UI |

Story Lifecycle 的独特定位：**"从需求到上线的全流程 AI 编排层"**。

```text
TAPD/Jira/Linear Story
        │
        ▼
  ┌─────────────────────────────┐
  │   Story Lifecycle Manager   │  ← 你在这里
  │   (Orchestration Layer)     │
  └──────┬──────────┬───────────┘
         │          │
    ┌────▼───┐ ┌───▼────┐
    │ Claude │ │ Qoder  │ ...   ← 任何 AI CLI
    └────────┘ └────────┘
         │          │
         ▼          ▼
      Code + Tests + Deploy
```

### 11.2 开源策略

- **MIT License** — 最大化采用率
- **单文件 SQLite** — `pip install` 后立即可用
- **Profile 市场** — 社区贡献 YAML 模板（"Java Spring Boot profile"、"React profile"）
- **Adapter 插件** — 社区贡献新 CLI adapter（Cursor CLI、Windsurf、等）

### 11.3 竞品对比（独一无二的卖点）

1. **CLI 无关**：不是另一个 AI 编码工具，而是管理所有 AI 编码工具的层
2. **多阶段编排**：从 PRD 到上线，不是单次对话
3. **Human-in-the-loop**：confirm 阶段人在 ttyd 里直接跟 AI 对话
4. **零配置启动**：SQLite + 本地模式，不需要服务器
5. **Profile 灵活**：3 阶段快速模式 ↔ 14 阶段完整模式
