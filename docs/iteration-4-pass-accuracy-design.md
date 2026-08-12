# 设计文档：放行准确率测量 + 全链路回放放量 + 人判校准（迭代 4，ABC 三线并行）

> 版本：v1.0（2026-08-12）｜ 状态：待评审 ｜ 作者：kimi 设计，opencode 执行
> 证据来源：迭代 3 报告（results/iteration3_20260812.md）§4 保真度限制结论、judge_context.py 证据链考古
> 前置纪律：judge 三元组（Go 端点 only）、pre-flight 端点检查、沙箱隔离、hc-all 只读、删除白名单制 + dry-run、冻结目录只读（severity 回补为已批准的一次性例外，下不为例）。

## 1. 背景与问题定义

迭代 3 完成裁判统一（回放测生产裁判 judge_stage_completion），36/36 应拦全拦。但暴露了回放保真度的根本限制：**沙箱测得出「拦截能力」，测不出「放行准确率」**。

裁判的证据菜单（judge_context.py）共七项，沙箱回放缺三项：

| 证据 | 生产 | 沙箱 |
|---|---|---|
| PRD / 成果物 / conformance / 决策历史 | ✓ | ✓（迭代 3 起齐） |
| 执行轨迹 session（adapter/attempt/outcome） | ✓ | ✗ 无真 agent 跑过 |
| 终端事件流（.story/runs/.../events.jsonl） | ✓ | ✗ 空 |
| 外部测试证据（test_report/provider） | ✓ | ✗ provider 未配置 |

迭代 3 的 4 条争议样本（align≥4 cov≥3 应放）全部因「无 test_report/执行证据」被拦——裁判不是觉得货不好，是无法证明货好，只能保守。议题 2（medium 自判阻断）因此无法关闭。

迭代 4 回答三个问题，对应三条线：
- **A**：证据给全时，裁判会不会冤枉好货？它判「拦」到底听内容还是听证据有无？（对照实验）
- **B**：整条流水线真实跑起来健壮吗？（野外实测 + token 常驻消费器）
- **C**：真实使用中，机判与人判的一致率多少？（长期真值校准）

**解读依赖关系**：B 的 gate 判决在 A 出结论前**只记录不解读**——A 是解读 B 的钥匙。

## 2. 目标与非目标

**目标**：
- G1（A 线）：三格对照实验落地，对「裁判是内容驱动还是证据驱动」给出可归因结论；议题 2 按预定决策规则关闭或升级；
- G2（B 线）：50 条历史 story 全链路真实执行回放，round 2 口径指标落盘；回放runner 常驻化（每晚定时 + 断点续跑 + 日预算护栏）；
- G3（C 线）：人判 vs 机判反馈机制上线（CLI 记录 + 数据 schema），开始攒真实混淆矩阵。

**非目标**：
- 不调 judge prompt（除非 A 线格 2 证伪 F2 集成，届时另立迭代）；
- 不改 scheduler/planner 编排；
- B 线不对 gate 判决下任何结论（A 落地前）；
- 不重跑 v2 基线评分；不动生产 story-lifecycle 的判定逻辑（C 线只加反馈记录，不改判决）。

## 3. A 线：模拟证据三格对照

### 3.1 证据模拟层（核心工程）

为沙箱回放补齐三项缺失证据，**全部样本使用同一套中性模板**——区分度只能来自 done_summary/conformance/参照物，不得来自模拟证据本身：

| 证据 | 模拟方式 |
|---|---|
| session 行 | 沙箱 DB 插入：adapter=opencode、attempt=1、outcome=success、artifacts_prod=该 stage 成果物清单（模板字段全样本一致） |
| events.jsonl | 沙箱 workspace 下 `.story/runs/<key>/pty_verify/events.jsonl`，写 20 条模板事件（命令执行/文件写入/测试运行，中性措辞，无质量暗示词） |
| test_report | verify stage 成果物目录落 `test_report.md`：模板「N passed / 0 failed」+ 文件清单（N 与清单从该样本 files_changed 派生，全样本同规则） |

**中性审计（验收 A1）**：对全部样本的模拟证据做 diff，除样本固有字段（story_key/文件清单）外必须逐字节一致；禁止出现 pass/fail 以外的任何质量评价措辞。

### 3.2 三格样本分配

| 格 | 样本 | 证据 | 期望 | 样本来源 |
|---|---|---|---|---|
| 格 1 | 合格 15 条 | 完整模拟 | **放行** | held-out 争议 4（e4fdff3243/99b052ac90/87f5faa863/2111abcf2b）+ abcd-A 4 + topic 放行 2 + v2 合格池（align≥4 且 cov≥4，种子 42 抽 5，排除已用键） |
| 格 2 | 应拦 19 条 | **同一套**完整模拟 | **仍拦** | replay_samples topic 应拦 19（v2 标签 align≤2 或 cov≤2，不含已迁移 2 条） |
| 格 3 | 应拦 36 条 | 缺证据 | 拦 | 迭代 3 已测完（36/36），直接复用，不重跑 |

格 2 是核心：conformance 说差（LOW）但一切证据说好（PASS）——裁判听谁的。

### 3.3 预注册决策规则（跑前定死，防事后解释）

- 格 1 放行率 ≥ 70% **且**格 2 拦截率 ≥ 80% → **裁判健全**：议题 2 关闭；B 线 gate 数据可解读；转 C 线长期校准；
- 格 1 放行率 ≥ 70% **且**格 2 拦截率 < 50% → **证据驱动实锤**：F2 内容信号未真正影响判决，立迭代 5 修 judge prompt 的证据权重（议题 2 升级为 P1）；
- 格 1 放行率 < 50% → **系统性保守**：证据全也拦，查 judge prompt 保守偏向（先分析 reason 文本聚类再定修法）；
- 中间地带（如格 2 拦 50-80%）→ 逐条读 reason 归因，如实报告，不强行归类。

成本：格 1+格 2 共 34 条 × 2 次调用（conformance 现场算 + judge）+ dry-run ≈ 80 次调用。

## 4. B 线：全链路真实执行回放放量

### 4.1 选样（50 条历史 story）

来源分层（种子 42，可复现）：
- snapshot_v2 `evidence/` 30 个证据目录中**未被 round 2 使用**的 10 条（samples20.json 去重校验）；
- story_refs 富化的优先批 19 条（有真实需求正文）；
- v2 conformance 池中 align≥4 的关联 story 抽 21 条（参照物齐全）。

分层约束：参照物类型（spec / prd / story_refs / tapd）与 单仓/跨服务 两维尽量铺开；选样脚本落 `dataset/b_batch50_20260812.json`，含与 round 1/2 全部历史样本的零重叠断言。

### 4.2 执行口径（沿用 round 2，便于纵向对比）

- harness：`pipeline_replay.py`，沙箱三件套（STORY_HOME 隔离 / AGENTS.md 防爬升 / hc-all 只读）；
- 指标：完成率、spec 质量分（ConformanceScore vs gold 参照物）、gate 判决（**只记录**）、耗时、token 消耗估计；
- 每条 story 独立 workspace + 独立沙箱 home；单条 15 分钟看门狗强杀（复用 54d92bd7 的 headless 看门狗）；
- Go 端点抖动：退避重试沿用 llm_client 既有机制；连续 3 条基础设施失败 → 暂停并报告，不硬闯。

### 4.3 常驻化（token 消费的底仓）

- runner 支持 `--nightly N`：每晚从候选池顺序取 N 条（默认 5），断点续跑（逐条落盘 jsonl，重跑跳过已完成键）；
- 日预算护栏：`EVAL_NIGHTLY_MAX=10` 硬上限 + 候选池耗尽自动停；
- 产出 append 到 `results/nightly_replay.jsonl`，每周汇总一节进 eval 周报（人工触发即可，不做自动推送）；
- 调度方式：Windows 计划任务或 kimi cron（实施时二选一，写明命令），不依赖 story-lifecycle 自身调度。

## 5. C 线：人判 vs 机判记录

- 新增 CLI：`story judge-feedback <story_key> <decision_id> <agree|disagree> [--note "..."]`，写 `judge_feedback` 表（story_key / decision_id / 机判 decision / 人判 / note / decided_at / created_at）；
- 机判数据现成（orchestrator_decision 表），反馈表只记人判侧；零 LLM 成本；
- 混淆矩阵口径：机判 approve + 人判 disagree = 漏拦；机判 reject/escalate + 人判 disagree = 误拦；月报脚本 `eval human-matrix`（读反馈表 + 决策表 join）；
- UI 按钮（QualityPanel 上加 agree/disagree）列为可选增强，本期不做——先用 CLI 攒数据，样本量证明价值再做界面。

## 6. SQL / 配置变更

- SQL：新增 `judge_feedback` 表（字段见 §5，含 created_at 索引）——**唯一的 schema 变更**，DDL 先落 `packages/eval/dataset/ddl_judge_feedback.sql` 证据，评审后执行；
- 配置：`EVAL_NIGHTLY_MAX`（默认 10）、nightly 调度项；无 Nacos 变更。

## 7. 验证方案（验收标准）

**A 线**：
- A1（中性审计）✓：全样本模拟证据 diff 通过（仅样本固有字段差异）；
- A2（三格结果）✓：格 1/格 2 数字落盘 `results/i4_abc_2026MMDD.jsonl`；按 §3.3 预注册规则给出结论；议题 2 状态明确（关闭/升级/归因中）；全程 Go 端点（pre-flight + grep api.deepseek.com 0 命中）。

**B 线**：
- B1（放量）✓：50 条完成率 ≥ 90%；沙箱审计三项全过（story.db 隔离 / hc-all git 零变化 / D:/hc-all/story 零写入）；spec 质量分落盘；
- B2（常驻化）✓：nightly runner 实测连跑 2 晚（可手动模拟触发）无人工干预完成；预算护栏实测超限即停；
- B3（纪律）✓：报告中 gate 判决只有数字罗列，无任何解读性结论（A 落地前的铁律）。

**C 线**：
- C1 ✓：`story judge-feedback` 全参数组合实测（agree/disagree/带 note/重复提交行为）；`eval human-matrix` 在空表上不崩、有数据时矩阵正确；
- C2 ✓：DDL 证据文件落盘，表结构与 §5 一致。

**报告**：`results/iteration4_2026MMDD.md`——commit 锚点、三线各自验收证据、A 线结论对 B/C 的解读影响、token 消耗实测（B 线逐条 + A 线总次）。

## 8. 风险与开放问题

1. **合成证据分布差**（A 线固有）：LLM 可能识别模板化文本并打折扣——如实记录为结论的星号，C 线的真实数据是最终校准；
2. **B 线 harness 抖动**：agent 执行受 Go 端点稳定性影响，失败样本标 infra_error 不计入指标，不静默丢弃；
3. **格 2 结果可能模糊**（拦截率 50-80%）：接受中间结论，逐条归因，不为凑二元结论硬掰；
4. **C 线冷启动**：依赖用户真实使用 story-lifecycle，样本积累慢——不催，月度盘点即可；
5. 开放问题：B 线 nightly 的调度宿主（计划任务 vs kimi cron）实施时定；judge_feedback 的 UI 化时机看样本量再说。
