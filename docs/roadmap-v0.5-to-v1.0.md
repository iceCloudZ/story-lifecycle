# v0.5.0 → v1.0.0 版本路线图

> 基于 `docs/` 全部设计文档与当前代码实现的真实差距。当前版本 v0.5.0。
>
> **服务端部署（ttyd / 后台守护 / 多租户等）属于 v2，不纳入此路线图。**

---

## 已完成（v0.5.0 已包含，无需重复规划）

以下内容已实现，**不出现在后续版本中**：

| 模块 | 关键文件 |
|------|----------|
| Headless / Zellij 共享抽象层 | `validation.py`、`artifacts.py`、`paths.py` |
| 可观测性（log_node_error / Debug API / TUI 门禁） | `observability.py`、`api.py` |
| 子故事 P0（DB + API + Service + workspace mutex） | `models.py`、`api.py`、`service.py` |
| SWE-bench Runner（clone cache / worktree / patch noise / eval harness） | `benchmarks/swebench.py`、`cli/swebench.py` |
| 路径收敛（`.story-done/` → `.story/done/` / prompt 更新 / doctor paths） | `paths.py`、`doctor_paths.py`、`prompts/` |
| `story demo` / `--dry-run` | `cli/demo.py`、`cli/main.py` |
| 质量飞轮 P0+P1（finding 生命周期 / checklist / packet / learned pattern） | `quality.py` |
| Review 门禁（GateDecision / review_round_count / gate report） | `gate.py` |
| Planner / Reviewer（plan_stage / review_stage / compress_context） | `planner.py` |
| 对抗循环（run_plan_loop / run_code_review_loop / detect_no_progress） | `evaluator_loop.py`、`loop_events.py` |
| LLM 语义提取（bug context / pattern matching / rerank / recovery） | `semantic.py` |
| StorySource 抽象（ManualSource / TapdSource / DB source_type+source_id） | `sources/base.py`、`tapd_source.py`、`models.py` |
| TUI 收件箱 `[i]` + 状态回写 TAPD | `tui.py`、`tapd_source.py` |
| Tool Registry（stage_tool / skill_tool） | `tools/stage_tool.py`、`tools/skill_tool.py` |
| trajectory_score 路由 | `planner.py` |

参考设计文档：

- `docs/design-headless-zellij-feedback-abstraction.md`
- `docs/design-swebench-runner.md`
- `docs/swebench-headless-debug-journey.md`
- `docs/story-observability-mvp-design.md`
- `docs/design-sub-story.md`
- `docs/story-quality-flywheel-design.md`
- `docs/design-review-gate-observability-and-control.md`
- `docs/design-smart-orchestrator.md`
- `docs/design-llm-semantic-extraction.md`
- `docs/design-story-source-integration.md`
- `docs/design-terminal-entry-lifecycle.md`
- `docs/design-foreground-zellij-execution.md`

---

## 总览

```
v0.6.0            v0.7.0            v0.8.0            v0.9.0            v1.0.0
    │                 │                 │                 │                 │
    │ 质量闭环        │ Engine 数据层    │ Domain 输入层    │ 双飞轮治理层     │ 生产就绪        │
    │ 架构审查 /     │ 梯度归因 /      │ 多模型对比 /    │ 项目智能 /      │ CI/CD /         │
    │ 阶段交接        │ 模式提取 /      │ PRD 增强 /      │ 飞轮晋升 /      │ 文档 /          │
    │                 │ 偏好数据集      │ 开放生态        │ 边界仲裁         │ 发布             │
```

---

## v0.6.0 — 质量闭环

**目标**：补齐 review 环节最后两块拼图——架构级审查和阶段间交接。质量飞轮的基础设施（finding / packet / learned pattern / gate）已在 v0.5.0 就位，v0.6.0 是让它们形成完整闭环。

当前代码对比：

- 已有：`quality.py`、`gate.py`、`review_feedback.py`、`seed_pipeline.py`、`semantic.py`，能支撑 finding、learned pattern、quality packet 和 review 语义摘要。
- 待补：`architecture_triggers.py` 尚未落地；stage handoff 目前仍停留在 idea/design 文档层，尚未进入 graph stage 边界和 prompt 渲染链路。

参考设计文档：

- `docs/idea-architecture-review-gate.md`
- `docs/engineering-architecture-review-triggers.md`
- `docs/idea-stage-handoff-package.md`
- `docs/story-lifecycle-ai-engineering-gap-roadmap.md`

### 架构审查门禁

| 模块 | 内容 |
|------|------|
| `architecture_triggers.py` | 检测多 Bug 指向同一抽象层失败 |
| Signal 收集 | Bug 分类 → 模式识别 |
| Packet 生成 | 结构化的架构审查请求 |
| TUI 展示 | 架构告警入口 |

### 阶段交接包

| 模块 | 内容 |
|------|------|
| Handoff 协议 | YAML frontmatter + markdown 结构定义 |
| LLM 自动生成 | 阶段完成时自动生成交接文档 |
| Review 校验 | 交接质量审查 |
| 注入到下级 prompt | Executor 拿到上一阶段的 handoff 作为上下文 |

---

## v0.7.0 — Engine 数据飞轮

**目标**：从 SWE-bench 实例执行中提取可复用的引擎改进信号。v0.5.0 已有完整的 prepare → solve → export → eval 流程，v0.7.0 补上分析和学习层。

当前代码对比：

- 已有：`story swebench prepare/solve/export/eval/summarize/run`，以及 clone cache、worktree、patch extraction、official harness 调用和 summary 输出。
- 待补：`story swebench analyze`、机器可消费的 trace sample、failure attribution、counterfactual candidate、preference dataset、pattern extraction 和 A/B 效果追踪。

参考设计文档：

- `docs/idea-swebench-data-flywheel.md`
- `docs/design-swebench-gradient-data-flywheel.md`
- `docs/design-swebench-runner.md`
- `docs/design-headless-zellij-feedback-abstraction.md`

### 梯度归因 + 反事实候选

| 模块 | 内容 |
|------|------|
| `story swebench analyze` | 单实例后分析，定位失败节点 |
| 归因报告 | 结构化输出失败原因分类 |
| 反事实候选 | 基于梯度信号生成改进方案 |
| 候选排序 | 预估改进收益排序 |

### 模式提取管道

| 模块 | 内容 |
|------|------|
| 约束提取 | 从失败实例提取通用约束 |
| 提示注入 | 约束注入到同类实例的 prompt |
| 效果追踪 | A/B 对比，验证注入是否提升 pass@1 |

### 偏好数据集生成

| 模块 | 内容 |
|------|------|
| 轨迹对比 | 好轨迹 vs 坏轨迹配对 |
| 数据集导出 | 标准格式，可复用于 router 训练 |
| 回归套件 | 固定实例集，每次改动必跑 |

---

## v0.8.0 — Domain 输入层 & 开放生态

**目标**：打通外部需求入口，降低新用户上手门槛。Story Source 基础抽象和 TAPD 对接已在 v0.5.0 就位，v0.8.0 提升输入质量和多模型验证能力。

当前代码对比：

- 已有：`StorySource`、`ManualSource`、`TapdSource`、`PrdProvider`、`TapdBodyPrdProvider`、`LocalFilePrdProvider`、`ShellAdapter`、`story demo`、doctor 中 Qoder/Gemini 检测。
- 待补：TAPD HTML 到 Markdown 的高质量转换、AI PRD 增强、LocalFilePrdProvider 的配置化/产品化、多模型并行对比、Qoder/Gemini 作为正式 adapter 的实现与文档。

参考设计文档：

- `docs/design-story-source-integration.md`
- `docs/idea-project-intelligence-pipeline.md`
- `docs/superpowers/specs/2026-05-23-story-source-p0.md`
- `docs/superpowers/plans/2026-05-23-story-source-p1.md`
- `docs/superpowers/specs/2026-05-21-story-lifecycle-v2-design.md`

### PRD 输入增强

| 模块 | 内容 |
|------|------|
| PRD 提取质量 | TAPD HTML→markdown 替换为 markdownify/html2text |
| AI 增强 PRD | 拉取时可选 LLM 优化 PRD 内容 |
| 本地文件 PRD | 已有 LocalFilePrdProvider，补配置化、错误提示和 TUI 可见性 |

### 多模型并行对比

| 模块 | 内容 |
|------|------|
| 并行执行 | 同一 story 多模型同时跑 |
| 结果对比 | 质量评分、diff 对比 |
| TUI 展示 | 对比面板 |

### 开放生态

| 模块 | 内容 |
|------|------|
| ShellAdapter 文档 | 已有 `adapters.yaml` 配置驱动实现，补文档和示例 |
| Adapter 测试基类 | 已有 adapter 单测，补新适配器复用模板 |
| 分层引导 | Quick Start → 进阶 → 自定义 |
| Qoder / Gemini CLI 适配器 | doctor 已检测，补上适配器实现 |

---

## v0.9.0 — 双飞轮治理层

**目标**：把 v0.6 的质量信号（domain）、v0.7 的引擎数据（engine）统一纳入双飞轮治理——定义隔离、检索、晋升、冲突仲裁和边界控制。

当前代码对比：

- 已有：`semantic.py`、`evaluator_loop.py`、`planner.py`、`tools/`、`trajectory_score` 和 review recovery 的基础能力，部分“智能引擎”骨架已经在 v0.5.0。
- 待补：Project Intelligence collector、Domain Asset / Outcome / Trace Maturity、Engine Strategy Registry、Shared Promotion Queue、domain/engine 检索边界、冲突仲裁和治理状态机。

参考设计文档：

- `docs/idea-dual-flywheel-domain-and-engine.md`
- `docs/idea-project-intelligence-pipeline.md`
- `docs/design-swebench-gradient-data-flywheel.md`
- `docs/story-quality-flywheel-design.md`
- `docs/idea-plan-review-adversarial-loop.md`
- `docs/superpowers/specs/2026-05-24-evaluator-optimizer-loop-design.md`

### 项目智能管道

| 模块 | 内容 |
|------|------|
| `RepoScannerCollector` | 代码库静态信号收集 |
| `TapdCollector` | 需求/Bug 动态信号收集 |
| Project Intelligence Packet | 注入 Planner prompt |
| 运行时信号 | 慢查询、错误日志检测 |

### 双飞轮治理

| 模块 | 内容 |
|------|------|
| Domain 治理 | Domain Asset / Outcome / Trace Maturity |
| Engine 治理 | Engine Trace / Strategy / Eval Evidence |
| 共享晋升队列 | `proposed → sandbox_validated → active` |
| 冲突仲裁 | `safety > domain production > engine execution > domain pattern > engine pattern` |
| 边界控制 | 原始业务数据不进 engine，原始 engine trace 不直接改 domain |

### 高级对抗循环收尾

| 模块 | 内容 |
|------|------|
| 结构化 findings 收敛 | 替代分数阈值判停 |
| Verification ladder | L0-L5 验证等级体系 |
| Debug recovery | LLM 驱动的恢复建议（已有 `semantic.recommend_recovery` 骨架，接入治理层） |

---

## v1.0.0 — 生产就绪

**目标**：达到可公开发布的质量标准。

当前代码对比：

- 已有：单元测试、部分 e2e scenario、SWE-bench runner、doctor、demo、Windows/Zellij 修复经验。
- 待补：全平台 CI、发布流水线、OpenAPI/README/CONTRIBUTING、稳定性回归套件、schema 迁移策略、安全审查和性能基准。

参考设计文档：

- `docs/e2e-test.md`
- `docs/roadmap-and-priorities.md`
- `docs/story-observability-mvp-design.md`
- `docs/design-terminal-entry-lifecycle.md`
- `docs/design-swebench-runner.md`

### CI / CD

| 模块 | 内容 |
|------|------|
| GitHub Actions | 全平台 CI（Windows / Linux / macOS） |
| 自动化测试 | lint + unit + e2e 完整流水线 |
| 发布流程 | PyPI 发布 + changelog 自动生成 |

### 文档

| 模块 | 内容 |
|------|------|
| README Quick Start | 5 分钟上手 |
| 完整 API 文档 | OpenAPI / Swagger |
| 架构文档 | 设计决策记录 |
| 贡献指南 | CONTRIBUTING.md |

### 稳定性

| 模块 | 内容 |
|------|------|
| 回归套件 | SWE-bench + 自定义用例 |
| 错误恢复 | 所有已知异常路径有恢复逻辑 |
| 向后兼容 | 配置文件 / DB schema 版本化迁移 |
| Windows CI | WSL + Git Bash 双通道修复 |

### 高级功能收尾

| 模块 | 内容 |
|------|------|
| 分支搜索 | MCTS 多路径探索 |
| 批量子故事 | 一键拆解大需求 |
| 嵌套子故事 | 子故事再拆子故事 |

### 最终审查

| 模块 | 内容 |
|------|------|
| 安全审查 | 全量代码安全审计 |
| 性能基准 | 大规模 story 并发测试 |
| 升级指南 | v0.x → v1.0.0 迁移文档 |

---

## v2 — 服务端部署（后续）

参考设计文档：

- `docs/idea-ttyd-server-side-web-terminal.md`
- `docs/superpowers/specs/2026-05-21-story-lifecycle-v2-design.md`

| 模块 | 内容 |
|------|------|
| ttyd Web 终端重连 | 执行模型统一、生命周期管理、安全认证 |
| 后台守护 | systemd / Windows Service |
| 多租户 | 用户隔离、资源配额 |
| Webhook 模式 | 外部事件触发 story |
| Jira / GitHub Issues | 其他平台适配器 |

---

## 版本依赖关系

```
v0.6.0 ──→ v0.7.0 ──→ v0.8.0 ──→ v0.9.0 ──→ v1.0.0
  │           │           │           │           │
  │           │           │           │           └── 最终发布
  │           │           │           └── 治理层需要数据层和输入层都就位
  │           │           └── Domain 输入层相对独立，可与 v0.7 并行
  │           └── Engine 数据层需要质量闭环的信号基础
  └── 质量闭环是两个飞轮的前置依赖
```

v0.7.0 和 v0.8.0 可并行推进，二者在 v0.9.0 汇合。
