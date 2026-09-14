# 文档索引与状态

> 规则：每个文档一行，标「状态 + 一句话说明 + 备注」。状态取值约定：**现行（权威）/ 已实现 / 已实施 v1.0 / 已冻结（冻结区）/ 已取代 / 已归档 / 思路记录 / 历史档案 / 待确认**。状态以各文件头部状态行为准逐个核对；文档与代码冲突时以代码 + [`ARCHITECTURE.md`](ARCHITECTURE.md) 为准。v2.0.0 起自动执行退役，原执行侧设计的归属见各行备注。

## 顶层文档

| 文件 | 状态 | 一句话说明 | 备注 |
|---|---|---|---|
| [ARCHITECTURE.md](ARCHITECTURE.md) | 现行（权威） | 5 层物理分层 codemap + 不变量 + 冻结范围 | 架构第一入口；v2.0 定位收口已更新 |
| [DESIGN-v1-work-agent.md](DESIGN-v1-work-agent.md) | 已实施 v1.0（七 WP）+ v2.0 增补 §13 | 工作特化 agent 定位宣告 + 自动执行退役收口 | v2.0 版本语义见 §13.4 |
| [FLOW-v1-work-agent.html](FLOW-v1-work-agent.html) | 现行 | 真实流程全景图：四泳道 × 八阶段 + ownership | 去重四重复点已转绿（①②消/③④缓，v2.0） |
| [DESIGN-story-butler.md](DESIGN-story-butler.md) | 已实施 WP1-WP6 | 管家系统：微信前门 + MCP 工具面 + 事件出口路由 | commits `c3d2eb20..07096870`（2026-09 上旬） |
| [DESIGN-task-actions-and-grill-me.md](DESIGN-task-actions-and-grill-me.md) | 拆分处置 | 动作清单 + grill-me 设计 | grill-me 已落地 skill 层（story-loop §1.8）；动作清单部分随 v2.0 自动执行退役冻结归档 |
| [DESIGN-artifact-driven-stage-completion.md](DESIGN-artifact-driven-stage-completion.md) | 已实现，冻结区 | done.json 砍掉，成果物落地即完成信号 | v3 经外部评审修订；执行侧随 v2.0 入冻结区 |
| [DESIGN-session-pty-id-model.md](DESIGN-session-pty-id-model.md) | 冻结区合约 | session/PTY ID 三模型 + capture 钩子契约 | 合约仍被 spawn 路径引用（AGENTS.md 域约定 §2.5） |
| [DESIGN-knowledge-flywheel-closure.md](DESIGN-knowledge-flywheel-closure.md) | 已实现（M1-M3） | 知识飞轮闭环：reflection / 挖掘触发 / 注入事件 | 遗留：真实 story 完成走查待 test-run |
| [DESIGN-consult-tool.md](DESIGN-consult-tool.md) | 已实现 | `story consult` — code agent 主动请外援（CLI 方案） | 实现走 engine FC 链，随 v2.0 入冻结区 |
| [DESIGN-docstore-remote-hosting.md](DESIGN-docstore-remote-hosting.md) | 思路记录 | story 文档托管服务器化（DocStore seam + ys-agent） | 未立项；真做时升格为正式设计 |
| [PLAN-proactive-cadence.md](PLAN-proactive-cadence.md) | 已执行（2026-08-14） | 每日 TAPD 同步 + 日程简报（`story daily`） | §12 有执行记录 |
| [PLAN-stage-confirm-gate.md](PLAN-stage-confirm-gate.md) | 待实施（历史/待确认） | 全自动链 stage 间确认闸 | 对象（全自动链）已随 v2.0 退役，大概率搁置 |
| [PLAN-dsh-absorption.md](PLAN-dsh-absorption.md) | 进行中（吸收清单） | dsh 不迁移、定期「抄柜子」的吸收点总表 | PROPOSAL 的姊妹篇，做完打勾 |
| [PROPOSAL-dsh-plugin-integration.md](PROPOSAL-dsh-plugin-integration.md) | 已决策：不采用（已归档） | story-lifecycle 作为 deepseek-harness 插件集成提案 | §0.2「护城河 vs 日用品」拆分仍有概念价值 |
| [REVIEW-dsh-plugin-integration.md](REVIEW-dsh-plugin-integration.md) | 已归档 | 上述提案的外部评审（以本机 dsh rc.6 实装为基准） | 结论已输入 PROPOSAL 决策块 |
| [DECISION-0-dsh-baseline.md](DECISION-0-dsh-baseline.md) | 已归档 | dsh 基线选择（已装 rc.6 vs master 源码） | 随提案「不采用」而失效 |
| [REFACTOR-orchestrator-three-layer-positioning.md](REFACTOR-orchestrator-three-layer-positioning.md) | 已采纳 | 编排器三层定位（Resolver/Decider/Handler） | 纪律已成 AGENTS.md「架构审查触发」硬规则 |
| [STORY-STATE-MODEL.md](STORY-STATE-MODEL.md) | 现行（权威） | Story 状态是独立第一公民，不从阶段派生 | 状态模型订正后的地基，其余 STATE-* 以它为准 |
| [STATE-CONSOLIDATION.md](STATE-CONSOLIDATION.md) | 已取代（档案） | 状态归一维护手册（4 真相源收敛） | 「派生」建模已订正（2026-07-09），以 STORY-STATE-MODEL 为准 |
| [STATE-DIAGRAMS.md](STATE-DIAGRAMS.md) | 档案 | 状态流程图（Mermaid） | 图 1-3 现状诊断仍准确；图 4-5 派生建模已订正 |
| [STATE-MAP.md](STATE-MAP.md) | 档案 | 真实状态机地图（现状还原，非理想设计） | 正文准确；末尾「北极星」节以 STORY-STATE-MODEL 为准 |
| [TABS-LIFECYCLE-STATE.md](TABS-LIFECYCLE-STATE.md) | 档案（记录中） | 四 Tab 与 lifecycle_state 对齐梳理 | append-only，保留推理痕迹 |
| [COORDINATOR-INTELLIGENCE.md](COORDINATOR-INTELLIGENCE.md) | 历史档案 | 协调 agent 智能化现状核查 RFC（2026-07-06） | 取证对象是已退役的自动编排链 |
| [BUGLOG-fullauto-walkthrough-20260710.md](BUGLOG-fullauto-walkthrough-20260710.md) | 历史事件 | 全自动 FC 流程人工走查 buglog（2026-07-10） | 多数条目随 v2.0 自动执行退役失效 |
| [PTY_WEBSOCKET_RECONNECTION_DESIGN.md](PTY_WEBSOCKET_RECONNECTION_DESIGN.md) | 历史/待确认 | PTY WebSocket 重连问题分析与产品方案 | 部分修复已在 0.11.x 落地（TDZ 自引用等） |
| [review-release-train-board.md](review-release-train-board.md) | 历史/待确认 | 班车看板 Code Review 交接修复清单 | 逐条修复完成度未核对 |

## 子目录

| 目录 | 说明 |
|---|---|
| `archive/` | 旧设计/ADR 存档（正文冻结，含 designs/ideas/plans/roadmaps） |
| `project-intelligence/` | 项目智能编号设计稿（01-09 + templates） |
| `prompts/` | prompt 资产（冻结可靠性环路审计等一次性 prompt） |
| `superpowers/` | plans + specs（superpowers 工作流产物） |
| `test-runs/` | 真实 story 跑测跟踪（总表 README.md + 每次一份 RUN-*.md） |
