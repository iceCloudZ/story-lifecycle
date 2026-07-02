# Story Lifecycle Manager

**Story 级 AI 编排器** — 把一个需求交给 AI，让它走完设计→实现→测试→审查的完整生命周期。

> 本包是 [`dev-flywheel`](https://github.com/iceCloudZ/story-lifecycle) monorepo 的一部分，与 [`packages/story-miner`](../story-miner) 共用统一知识飞轮。当前版本：**v0.12.0**。

## 安装 & 快速开始

在 monorepo 根目录（推荐）：

```bash
cd ..
python -m venv .venv-monorepo-test
source .venv-monorepo-test/Scripts/activate   # Windows Git Bash
pip install -e packages/story-lifecycle
pip install -e packages/story-miner
pip install -e packages/knowledge

story setup           # 配置 LLM API Key（必填）
story demo            # 0 依赖体验完整流程
story serve           # 启动编排服务 (localhost:8180)
story                 # 打开 TUI 面板
```

单独安装：

```bash
pip install story-lifecycle
```

## 与 story-miner 的集成（v0.12.0+）

- **I1 定时扫描**：`story-miner` 通过 `scripts/refresh.sh` 每日增量/每周全量扫描本地 transcript。
- **I2 精确绑定**：本包在 `inject_prompt` 时写 `anchors.jsonl`，`story-miner` 优先用锚点把 session 绑到 story，hc-all 工作区 story-sign 会话绑定率 80.4%。
- **I3 历史上下文注入**：`design/build/verify` prompt 自动注入 `{transcript_context}`（来自 `story-miner` 的 `TranscriptStoryContextProvider`）。
- **I4 Done 复盘**：`story done <key>` 自动调用 `story-miner/scripts/retrospect.py --story <key>` 生成合并复盘。

详见顶层 [`docs/INTEGRATION.md`](../docs/INTEGRATION.md) 与 [`docs/ADOPTION.md`](../docs/ADOPTION.md)。

## 核心理念

> ⚠️ **本节及下方"对抗循环"段描述的是 LangGraph 时代的 Planner/Reviewer 三角色架构，已于 cb6f9cd (2026-06-13) 被 Function Calling 模式取代。** 三角色在 FC 下被 LLM 内化（非独立函数）。当前真实架构见 [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md)。本段保留作历史背景，待重写。


每个 Story 都经历多个阶段（design → implement → test → review），每个阶段由独立的 AI 会话处理。编排器负责：

- **规划**：每阶段开始前，Planner LLM 分析 story 上下文，生成任务书
- **执行**：AI CLI（Claude Code / Codex / Kimi 等）在隔离会话中工作（python 自管 CLI 进程，`terminal/pty.py`）
- **审查**：Reviewer LLM 审查阶段产出，识别问题
- **路由**：LLM 决定 advance / retry / skip / fail

```
STORY-123 "Add dark mode"
  ├─ [plan:design]    Planner → 任务书
  ├─ [execute:design] Claude Code → spec + 复杂度评估
  ├─ [review:design]  Reviewer → 审查意见
  ├─ [plan:implement] Planner → 编码任务书
  ├─ [execute:implement] Claude Code → 代码修改
  ├─ [review:implement] Reviewer → 代码审查
  └─ [execute:test]   Claude Code → 测试验证 → 完成
```

## 对抗循环（v0.5.0+）

编排引擎内置双层对抗循环（`evaluator_loop.py`），是质量保证的核心机制：

### Plan ↔ Review 循环

执行计划不是一次生成的——Planner 产出计划后，Reviewer 立即审查：

```
plan → review → revise → review → pass
  │                            │
  └── 最多 3 轮 ──────────────→ wait_confirm (人工介入)
```

每轮审查检查：范围覆盖、上下文完整性、可行性、风险点。Reviewer 不满意就打回重来。

### Code ↔ Review 循环

代码写完后同样经过对抗审查：

```
execute → review → revise → review → pass
   │                           │
   └── 最多 3 轮 ────────────→ wait_confirm
```

### 收敛条件

- `pass`：无阻塞性问题，推进
- `revise`：有 high/major 问题，打回修改
- `no_progress`：连续无改善，自动终止等人工
- `max_rounds`：达到上限，自动终止等人工

FC 模式下，"对抗循环"由 LLM 内化：gate 返回 retry 时，planner 往 action 队列插入新 launch action，由 LLM agent 自行重跑修复（无独立 Python review loop 函数）。收敛靠 gate 硬闸（`round_count > max_retries` 强制 fail）。详见 [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md)。

## 质量飞轮

系统会从每次审查中沉淀可复用的质量经验：

```
审查发现 Finding → 接受 → 修复 → 验证 → 学习为 Learned Pattern
                                                      │
新 Story ─────────────────── 模式复发检测 ──────────────┘
```

- **Finding**：每次审查产出的问题记录（open → accepted → fixed → verified → learned）
- **Quality Packet**：story 启动时注入相关的历史 pattern 和检查清单
- **Pattern Recurrence**：新 issue 自动匹配已知 pattern，检测复发

## 子 Story 拆分

复杂需求自动拆分为独立子任务：

```
STORY-100 (L: "重构认证系统")
  ├─ STORY-100-auth (M) → depends on: none
  ├─ STORY-100-api (S)  → depends on: STORY-100-auth
  └─ STORY-100-test (M) → depends on: STORY-100-auth, STORY-100-api
```

子 story 共享父 story 的知识库，有依赖关系的按序执行，无依赖的并行跑。

## SWE-bench Runner

内置 SWE-bench 评估管线，用于批量测试和引擎改进：

```bash
story swebench run \
  --instances sweep-verified.jsonl \
  --run-id my-run \
  --budget smoke \
  --agent claude
```

完整流程：prepare → solve → export → eval → summarize

## TUI 面板

```
story    # 启动 TUI

[n] 创建 Story       [N] 创建子 Story    [i] 收件箱
[e] 进入 AI 会话     [s] 跳过当前阶段     [f] 标记失败
[r] 恢复             [a] 中止             [?] 帮助
```

TUI 展示每个 Story 的状态、当前阶段、执行次数、轨迹评分。

## 配置

### Profile（流程定义）

```yaml
# profiles/minimal.yaml
stages:
  design:
    order: 1
    description: "需求分析与方案设计"
    review: true          # 启用对抗审查
    max_retries: 2
    expected_outputs: [spec_path, complexity]
  implement:
    order: 2
    review: true
    max_retries: 3
    expected_outputs: [files_changed, summary]
  review:
    order: 3
    review: false
    expected_outputs: []

quality:
  enabled: true
  inject_quality_packet: true
  inject_executor_checklist: true

adversarial:
  enabled: true
  plan_loop:
    enabled: true
    max_rounds: 3
  code_loop:
    enabled: true
    max_rounds: 3
```

### LLM Provider

```bash
story setup   # 交互式配置
```

支持：DeepSeek（默认）、Anthropic、OpenAI、智谱 GLM、自定义 OpenAI 兼容端点。

### Adapter（AI CLI）

```yaml
# ~/.story-lifecycle/adapters.yaml
my-tool:
  launch_cmd: "my-cli --model {model}"
  inject_method: stdin
```

## 架构

story-lifecycle 是 Function-Calling 编排引擎，**5 层物理分层（ISS-012，目录 = 逻辑）**：①`entry`(cli/web/profiles) → ②`sourcing`(sources/planner/integrations) → ③`orchestrator`(FC agent 规划+执行+gate 硬闸) → ④`knowledge`(context_providers/adapters/knowledge_store) → ⑤`infra`(config/db/terminal/prompts/...)。依赖方向自上而下，从目录即可读出。

两种工作流并存：**全自动**(FC agent 规划 → 前端确认 HITL → 后台执行 + verify gate) / **半自动**(release_prompt 模板 → 人工拷贝 CLI)。

本包是 `dev-flywheel` monorepo 一部分，与 `story-miner`(生产) + `knowledge`(契约) + `testing`(E2E) 协作。跨包知识飞轮靠 2 个 SOFT 缝运转。

**完整架构、codemap、不变量见 [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md)。**

## CLI 参考

```
story                          TUI 面板
story demo                     模拟完整流程
story create <KEY> -t <TITLE>  创建并启动 story
story create <KEY> --dry-run   预览 prompts
story serve                    启动 API 服务器 (8180)
story setup                    配置 LLM
story doctor                   环境检查
story swebench run             SWE-bench 评估
story review-feedback import   导入审查反馈
story review-feedback list     查看 findings
story approvals list           待审批队列
```

## 平台支持

| 平台 | CLI + TUI | AI 执行 |
|------|-----------|---------|
| Linux | ✓ | python pty |
| macOS | ✓ | python pty |
| Windows | ✓ | python pty (pywinpty, Git Bash) |

## License

MIT

