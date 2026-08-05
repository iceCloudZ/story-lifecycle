# eval 迭代循环任务：gate 回测 + 失败模式挖掘（第一轮）

> 自包含任务文档。执行者：opencode。写于 2026-08-05，基于冻结快照 `dataset/snapshot_20260805/`。
> 目的：把已收敛的 eval 语料变成 story-lifecycle 的迭代驱动器。本任务只做**测量**，不改 story-lifecycle 任何行为。

## 0. 铁律

- 一切输入来自 `D:/github/story-lifecycle/packages/eval/dataset/snapshot_20260805/`，**不得**读 `dataset/` 活数据（活数据会继续演进，replay 必须可复现）。例外：C 源 spec/PRD 按 stories_matched.jsonl 的 evidence_dir 从 `D:/hc-all/story/` 活目录读（快照未含 C 源），因此必须把实际使用的参照物内容随明细存档（见 §1.3）。
- **LLM 端点：一律 opencode-go**（`https://opencode.ai/zen/go/v1`，环境变量 `OPENCODE_API_KEY`，模型 `deepseek-v4-flash`）。**禁止使用 DeepSeek 官方端点**（`dataset/.env.deepseek` 仅人工特批场景用，本任务不在其列）；judge prompt 不许动。
- **不得修改 story-lifecycle 核心包的任何代码**（gate/verify/intake/prd_generator 等）。本任务以只读方式 import 调用它们；新代码只许写在 `packages/eval/src/eval/` 下。
- 产出只落 `packages/eval/results/` 和本任务文档指定位置，不外发、不推钉钉。

## 1. 任务 A：gate 回测（拦截率基线）

**问题**：story-lifecycle 的 gate/verify 环节，能不能在「交付前」拦住历史上真实发生的 drift？

### 1.1 样本集（从快照构造）

- **正样本（应拦截）**：快照 merge_scores.jsonl 里 conformance.alignment ≤ 2 且有关联参照物的 merge（含「真 drift」与疑似错链，预期 30-50 条）。
- **对照样本（不应拦截）**：alignment ≥ 4 的 merge，随机取与正样本等量（控制误拦率）。
- 每条样本需组装：参照物（优先级 spec > prd > story_refs > tapd 描述，复用 scanall 现有逻辑）+ merge diff（ deliveries.jsonl 里有 hash，git 仓库在 D:/hc-all/<repo>，merge 必须显式 `diff <merge>^1 <merge>`）。

### 1.2 回测方法

1. 在 story-lifecycle 项目里定位 gate/verify 的代码入口（`unified_gate` 或交付校验相关模块），搞清楚它的输入签名（需要什么规格的需求文档 + 什么格式的交付物）。
2. 写 `packages/eval/src/eval/gate_replay.py`：对每条样本，把历史 spec/参照物 + diff 适配成 gate 的输入格式，调用 gate，记录：pass/fail、findings、耗时。gate 输入不适配的样本记 `skip: <原因>`，不许硬塞。
3. 若 gate 本身依赖 story.db 运行时状态（C:/Users/zzh58/.story-lifecycle/story.db），只读使用；依赖不成就对该样本记 skip。

### 1.3 产出与验收

- `results/gate_replay_20260805.md`：
  - 总表：正样本拦截率（应拦且拦 / 应拦总数）、对照误拦率、skip 数与原因分布；
  - 明细表：每条样本一行（merge、tapd、eval 判定的 alignment、gate verdict、gate findings 摘要、是否一致）；
  - 不一致 case 分析：gate 放行的 drift（漏拦）挑 5 条分析 gate 为什么没发现（是输入信息缺失还是规则缺失）；误拦的对照挑 3 条分析。
- 报告头部必须记录版本锚点清单：
  - 快照名（snapshot_20260805）+ eval 代码 git commit + judge 模型/端点（应为 opencode-go）；
  - **工作区版本**：story-lifecycle 工作区 HEAD commit + `git status --short` 摘要（含未提交改动，工作区即被测版本）；样本涉及的每个 `D:/hc-all/<repo>` 的 HEAD commit（`git -C D:/hc-all/<repo> rev-parse HEAD`）；
  - **参照物留痕**：每条样本把实际使用的参照物内容（spec/prd/story_refs/tapd 描述，取优先级最高者）原文存入 `results/gate_replay_refs_20260805/<repo>_<merge前10位>.md`——C 源 spec 读的是活工作区文件，不存档将来无法复现这次评判。
- 将来 gate 改进后 replay，与本报告对比即得归因干净的 delta。
- 验收：明细行数 = 正样本+对照-skip；拦截率/误拦率可从明细表直接复算；`git status` 确认 story-lifecycle 核心包零改动。

## 2. 任务 B：失败模式挖掘

**问题**：低分交付有没有系统性规律？每个规律应对应 story-lifecycle 的一个可改进点。

### 2.1 方法

基于快照做离线分析（`packages/eval/src/eval/failure_mining.py` 或 notebook 脚本均可）：

1. 取 alignment ≤ 2 的全部 case，按 findings/summary 做主题聚类（可用 LLM 聚类，走 opencode-go 端点 `https://opencode.ai/zen/go/v1`，环境变量 OPENCODE_API_KEY；并发 ≤8）；
2. 对每个聚类给出：模式命名、涉及 merge 数、典型 case 3 条、跨维度分布（repo/月份/管线内外，deliveries 里有 story_key 可查）；
3. 已知模式做校验而非重新发现：「参照物缺失」（应已大幅缓解）、「跨服务需求单仓切片」、「先开发后补录需求单」、「同域概念混淆错链」——报告里确认这些模式各占多少、是否还有残余。

### 2.2 产出与验收

- `results/failure_patterns_20260805.md`：模式清单（按 merge 数排序），每个模式附「对 story-lifecycle 的改进建议」一段（建议要具体：哪个环节、加什么检查/约束）。
- 验收：聚类覆盖全部 alignment≤2 case；每个模式的典型 case 可在 merge_scores.jsonl 里复查到；改进建议条数 = 模式数。

## 3. 完成定义

1. 两份报告落盘且通过各自验收项；
2. `git status` 证明 story-lifecycle 核心包零改动、packages/eval 之外零改动；
3. 回复里给出：gate 拦截率 / 误拦率 / skip 分布；失败模式 top5 及各自 merge 数；两份报告路径。

## 4. 后续（不在本任务内，供人参考）

本任务的产出将决定 story-lifecycle 的第一批真实改动：漏拦率高的模式 → gate 加规则；参照物缺失残余 → intake 拦截；等等。改动完成后按本任务同样方法 replay 一轮，对比拦截率 delta ——那才是一次完整的迭代循环。
