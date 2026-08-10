# 设计文档：UI 主链路修通 + 质量结果可见 + judge 输入核查（迭代 2）

> 版本：v1.0（2026-08-06）｜ 状态：待评审 ｜ 作者：eval 体系产出，opencode 执行
> 证据来源：round 3 UI E2E（`packages/eval/results/ui_e2e_20260805.md`）、迭代 1（F1/F2 已上线，commit 2b4db820）
> 前置纪律：judge 三元组（Go 端点 only）、pre-flight 端点检查、沙箱隔离、hc-all 只读、删除操作白名单制。

## 1. 背景与问题定义

迭代 1 修好了 verify 的「脑子」（fail-closed + conformance 质检），round 3 的 UI 驱动测试证明「脸和手脚」有三处断裂：

**P4-UI（主流程断）**：UI 点「开始执行」无法启动 agent——`/advance` 不设置 plan 确认态，「确认规划」按钮的显示条件（`status === 'planning'`）与实际状态机不匹配（前端证据：OverviewTab.tsx:147、TerminalTab.tsx:210、StoryDetailPage.tsx:120）。写路径实测被迫降级 API 才能跑。产品主链路对真实用户是断的。

**P2-UI（质量结果不可见）**：gate-history API 存在但前端零调用——gate decision/verdict/findings/repair_action、escalate、[FALLBACK]、conformance 分数在 UI 上零呈现。迭代 1 的修复价值无法触达用户。

**P6-JUDGE（质检员误判）**：写路径实测 spec.md 完整落地（18KB）但 judge 判「产出为空」→ 误 reject → escalate。疑似 judge 的产物路径解析与实际落点（`story/<sid>-<slug>/spec.md`）不一致——P2（迭代 1）修了 profile 契约，judge 的取件路径未同步核查。

## 2. 目标与非目标

**目标**：
- G1：UI 创建 → 确认规划 → 开始执行 → agent 真实启动，全流程可点通，状态实时可见；
- G2：story 详情页可见完整质量信息：gate 决策历史、findings（含严重度）、repair_action、escalate/[FALLBACK] 标记、conformance 分数；
- G3：judge 取件路径与实际落点统一，产出存在时零误判「为空」，产出真空时仍能正确拒绝（双向不错）。

**非目标**：不改 planner/scheduler 编排逻辑；不做 UI 改版美化（只补功能缺口）；不动 eval 包（Playwright 回归脚本复用 round 3 的）。

## 3. P4-UI：启动链路修通

### 3.1 先探明（实施第一步，结论写入报告）

梳理规划确认状态机的**设计意图**：`status` 实际取值序列（planning/planned/confirmed/…）、`_plan_confirmed` 在后端的读写点、`/advance` 的职责。三个前端条件点（OverviewTab:147 / TerminalTab:210 / StoryDetailPage:120）各自期望什么。

### 3.2 设计（契约统一）

- 后端：story detail API 响应显式返回 `plan_confirmed: bool`（数据源唯一，前端不再从 status 字符串推断）；`/advance` 在规划已完成时设置确认态并触发编排启动，返回新状态。
- 前端：三个条件点统一改为读 `plan_confirmed`；「开始执行」按钮可用条件 = 规划完成 && !confirmed && 无运行中 stage；点击后禁用 + 显示执行中状态（refetch 间隔逻辑 StoryDetailPage:120 同步修正）。
- 兼容：status 字符串保留原语义不删（其他消费方不动），plan_confirmed 为新增字段。

## 4. P2-UI：质量面板

- 后端：核查 gate-history API 响应是否含全量字段（decision/verdict/reason/findings 列表/repair_action/fallback 标记/conformance 分数/时间戳）；缺则补，不改表结构（gate_result/finding 表现有字段映射即可）。
- 前端：StoryDetailPage 新增「质量门禁」面板（放 OverviewTab 或独立 tab，按现有组件风格）：
  - 决策时间线：逐条 decision + verdict + reason + 时间，escalate 高亮、[FALLBACK] 打标；
  - findings 列表：severity/category/description/location，HIGH 置顶；
  - conformance 分数：alignment/coverage/scope_drift 三维（有则显示）；
- 空态：无 gate 记录时显示「尚未经过 verify」，不报错。

## 5. P6-JUDGE：取件路径统一 + 误判分类

1. **路径审计**：列出所有解析产物路径的消费点（stage_completion judge、conformance.check_conformance 的 spec_path、profile_loader artifact_check、其他 judge），与实际落点（`story_evidence_root(workspace)/<sid>-<slug>/spec.md`）逐一比对；
2. **统一解析**：收敛到单一解析函数（复用/扩展 story_paths 的产物定位，支持 `<sid>-<slug>` 目录形态 + 旧 `story/spec.md` 兜底）；所有 judge 改走该函数；
3. **误判分类**：judge 输入构造时区分「产物不存在」（→ escalate，reason 明确为路径/落点问题，不进 reject 重试循环）与「产物为空/内容不合格」（→ 正常 reject）；杜绝把「找不到」当「写得差」。

## 6. SQL / 配置变更

- SQL：无变更（全部走现有表字段映射与 API 响应组装）。
- 配置：无新增配置项。前端构建产物（entry/web/assets）按仓库既有惯例处理，构建前确认 git status 中已删除的旧 assets 与本次构建的关系，不混入无关变更。

## 7. 验证方案（迭代 2 验收标准）

基于 v2 快照沙箱 + round 3 Playwright 脚本（扩写为回归资产），LLM 走 Go、pre-flight 先验端点：

1. **G1 主链路 E2E**：Playwright 全流程——UI 创建 story（用 v2 样本 gold PRD）→ 规划完成 → 「确认规划」按钮出现且可点 → 「开始执行」点击 → agent 真实启动（pty 日志出现）→ 状态推进 UI 可见。全链路无 API 降级。
2. **G2 质量面板**：v2 construct 三类样本（HIGH finding / swap / 缺依赖）+ 1 条 escalate 样本种入沙箱，UI 逐项断言可见（决策时间线、findings、[FALLBACK] 标记）；截图留证。
3. **G3 judge 双向不错**：(a) spec 正常落地 → judge 不得判「产出为空」；(b) 人为制造空产物 → judge 正确 reject；(c) 产物路径故意放错 → escalate 且 reason 指明落点问题，不进 reject 循环。
4. **回归**：round 3 读路径检查全过；round 1 gate 回测抽 20 条无异常；`git status` 无 eval 包外无关改动。
5. **版本锚点**：报告头部 story-lifecycle 新 commit + 快照 v2 + judge 三元组。

## 8. 时序与协调

改动面：`api.py`（detail/advance/gate-history）、`frontend/src/`（三个条件点 + 新面板）、`orchestrator/evaluation/` + `infra/story_paths.py`（取件解析）。与 planner 重构面（engine/scheduler）正交。前端构建依赖若缺失（node_modules），在仓库既有 frontend 目录内安装，不动全局环境。

## 9. 风险

- **状态机理解偏差**：P4-UI 探明结论若显示「确认规划」是刻意的人工 gate（不是 bug），则改为「让按钮可达且文案清晰」而非绕过确认——以探明结论为准实施。
- **前端零测试现状**：Vue 前端无既有单测，验收全靠 Playwright E2E；面板字段断言写成 data-testid 锚点，降低选择器脆弱性。
- **judge 误判样本少**：P6 目前只有 1 个实测 case，路径审计若发现另有成因（如 done_data 未声明产物），按实际根因修并在报告中修正本节。
