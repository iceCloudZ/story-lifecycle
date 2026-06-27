# 编排 LLM 必填化

## 背景

Story Lifecycle 的编排器 LLM（通过 `STORY_LLM_API_KEY` 配置，默认使用 DeepSeek）目前是**可选**的。当用户未配置 API Key 时，各模块存在 rule-based 回退路径：

| 模块 | 当前回退行为 |
|------|-------------|
| `router.py` | 已强制要求 LLM（`route()` 无 key 时 `raise RuntimeError`） |
| `planner.py` | `is_available()` 返回 False → `nodes.py` 使用 profile 静态配置生成计划 |
| `semantic.py` | 正则匹配代替 LLM 做 Bug 上下文提取、模式复发检测、审查摘要 |
| `review_feedback.py` | Bullet-list 解析代替 LLM 从审查 markdown 中提取 findings |
| `evaluator_loop.py` | Fallback 模式跳过对抗循环 |

## 问题

Rule-based 回退路径存在三个根本性问题：

### 1. 质量差距不可接受

| 能力 | LLM | Rule Fallback |
|------|-----|---------------|
| Bug 上下文提取 | 理解语义关系、因果链 | 正则匹配关键词，无上下文理解 |
| 模式复发检测 | 语义相似度匹配 | 关键词交集，大量假阳性/假阴性 |
| 审查 findings 提取 | 理解审查意图，结构化输出 | Bullet-list 解析，对非标准格式完全失效 |
| 阶段计划生成 | 根据 story 内容定制 | 静态 profile 模板，所有 story 一样 |
| 路由决策 | 多维度权衡 | 已强制 LLM |

### 2. 用户无法感知降级

回退是静默的。用户不知道自己的 story 走的是"劣化版"还是"完整版"——TUI 上一个小字 `rule_fallback` 不足以引起注意。用户可能始终不知道自己配置了 LLM 与否，产出的质量差别有多大。

### 3. 维护负担

两套代码路径需要同步维护：

- `semantic.py` 同时维护 LLM prompt + 正则表达式
- `review_feedback.py` 同时维护 LLM 提取 + bullet-list 解析
- `nodes.py` 同时维护 LLM 计划 + 静态 fallback 生成

每次改 LLM prompt 或输出 schema，都要考虑 fallback 是否还兼容。实际上 fallback 路径的测试覆盖率远低于 LLM 路径。

## 决策：必填化

移除所有 rule-based 回退路径，编排 LLM 变为**必填**。理由：

1. **LLM 成本已可忽略** — 默认 DeepSeek，路由/规划/语义提取都是轻量调用（单次 <1000 token），单 story 完整生命周期成本 <¥0.01
2. **6 个大版本迭代已验证 LLM 稳定性** — v0.1.0 → v0.5.1，LLM 路径是主力执行路径
3. **回退路径的实际使用率极低** — 仅 demo / 测试 / 首次启动前的短暂窗口使用
4. **简化实现** — 删除回退代码后，每个模块减少 30-50% 代码量

## 运行时 LLM 异常处理

必填化只影响**启动期可用性判断**（去掉 `is_available()` 守卫和 rule-based 回退），**不删除运行时异常处理**。LLM 在运行时仍然可能失败（超时、429 限流、服务宕机等），以下是各模块的预期行为：

### 分级处理策略

| 场景 | 模块 | 失败影响 | 预期行为 |
|------|------|----------|----------|
| 路由决策 | `router.py` | 关键路径：决定 stage 下一步走向 | `try/except` → `log_route_decision` → 标记 `_next_action = "fail"`，story 进入 `wait_confirm`，**不崩溃** |
| 阶段计划 | `planner.py` | 关键路径：无计划则 Executor 不知道做什么 | `try/except` → `log_node_error` → 标记 `_block_for_planner` → story 进入 `wait_confirm`，**不崩溃** |
| 审查 | `planner.py` | 重要但非阻塞：跳过审查 stage 可继续 | `try/except` → 记录 warning → 返回 state，跳过本次审查，**不崩溃** |
| 对抗循环 | `evaluator_loop.py` | 重要但非阻塞：可降级为普通审查 | `try/except` → 回退到普通 `review_stage` 调用，**不崩溃** |
| 模式复发检测 | `semantic.py` | 增强功能：不匹配 pattern 不影响核心流程 | `try/except` → 记录 warning → 返回空匹配列表，**不崩溃** |
| 语义提取 | `semantic.py` | 增强功能：提取失败返回 error result | `try/except` → 返回 `_error_result()`，调用方自行处理，**不崩溃** |

### 统一原则

1. **进程不崩溃** — 任何 LLM 调用失败都不会导致 `story serve` 进程退出
2. **状态不回退** — 失败时 story 进入 `wait_confirm` 或 `fail`，不隐式降级到劣化行为
3. **错误可观测** — 所有失败通过 `log_node_error` / `log_route_decision` / `log_event` 写入 DB，TUI 可见
4. **调用方可重试** — 用户在 TUI 中看到失败原因后，可以手动 retry

### 当前已就位的运行时保护（不删除）

- `router.route()` — `try/except` + `log_route_decision`（L1549-1568）
- `planner.plan_stage()` — `try/except` + `log_node_error`（L430-450）
- `planner.review_stage()` — `try/except` + warning（L852-853）
- `match_pattern_recurrence()` — `try/except` + warning（L986, 1015）

> 注意：`match_pattern_recurrence` 当前在异常时回退到 keyword matching（L1016-1032），本次改动会将此回退移除，改为仅记录 warning 并返回空匹配。

## 数据隐私与合规

编排 LLM 默认使用 DeepSeek API（`api.deepseek.com`），所有阶段计划、审查内容、语义提取和路由决策的 prompt 会发送到 DeepSeek 服务器。

对于有数据合规要求的企业用户：

- **配置本地部署端点**：`story setup` 支持自定义 provider，选择 "custom" 并填入企业内部的 OpenAI 兼容 API 端点（如私有化部署的 vLLM、Ollama、LocalAI 等），数据不出企业网络
- **`base_url` 配置**：在 `~/.story-lifecycle/config.yaml` 中修改 `base_url` 指向内部端点即可，无需改代码
- **注意**：AI 执行器（Claude Code / Codex CLI）使用各自的 API Key 和端点，与编排 LLM 独立配置

## 不在范围内的

- **AI 执行器（Claude Code / Codex CLI 等）** — 这些是故事执行层，使用各自的 API Key，不受编排 LLM 配置影响
- **`story demo`** — demo 使用 mock planner，不需要真实 LLM，保持现状

## 影响范围

### 需要修改的文件

| 文件 | 改动类型 |
|------|----------|
| `orchestrator/planner.py` | 删除 `is_available()` |
| `orchestrator/router.py` | 删除 `llm_is_available()` |
| `orchestrator/loop_events.py` | 删除未使用的 `log_loop_fallback()` |
| `orchestrator/nodes.py` | 删除 `_planner_policy()`、`is_available` 守卫、static fallback 计划生成 |
| `orchestrator/semantic.py` | 删除 6 个正则回退函数、所有 `if not _get_api_key()` 检查 |
| `orchestrator/review_feedback.py` | 删除 bullet-list 解析回退 |
| `orchestrator/evaluator_loop.py` | 删除未使用的 `fallback` 字段 |
| `cli/main.py` | `_run_server()` 无 key 时 `SystemExit(1)` |
| `cli/tui.py` | 删除 `llm_is_available()` 状态判断 |
| `cli/seed_quality.py` | 删除 `is_available()` 检查 |
| `cli/demo.py` | 更新 mock |
| `profiles/demo.yaml` | 删除 `planner:` 配置块 |
| `profiles/headless-smoke.yaml` | 删除 `planner:` 配置块 |
| `tests/` （5 个文件） | 删除所有 `is_available` / `llm_is_available` mock |

### 不删除的

- `nodes.py` 中 `try/except` — 运行时错误（网络超时等）仍需处理
- `semantic.py` 空输入提前返回 — 合法逻辑分支
- `review_feedback.py` JSON 解析路径 — 合法优化，非 LLM 回退
- `evaluator_loop.py` profile 中的 `fallback` key — 向后兼容

## 用户体验变化

**之前**：`story serve` 无 API key 时静默启动，走劣化路径
**之后**：`story serve` 无 API key 时报错退出，提示运行 `story setup`

**之前**：`story` TUI 显示 `router: disabled`
**之后**：`story` TUI 始终显示 `router: enabled (deepseek)`

**之前**：`story demo` 需要 mock `is_available = False`
**之后**：`story demo` mock 完整 planner 返回值，无需特殊处理

## 迁移指南

对于已有用户：运行 `story setup` 配置 API Key 即可。如果没有 API Key，DeepSeek 注册即送免费额度，足够日常使用。
