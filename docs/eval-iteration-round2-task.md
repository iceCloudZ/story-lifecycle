# eval 迭代循环任务：全管线回放（round 2）

> 自包含任务文档。执行者：opencode。写于 2026-08-05（v2，基于架构探明结果重写）。
> 前置：round 1 已完成（gate 回测 100% 拦截 / 误拦主因口径差异；失败模式 top 为覆盖不足类）。
> 目的：把历史上**没走 story-lifecycle** 开发的需求，用管线回放一遍（intake→PRD→spec→verify，摘除 build），回答：管线跑得完吗、生成的 spec 靠谱吗、gate 端到端拦得住吗。

## 0. 架构事实（已探明，照此设计，勿另起炉灶）

- **回放设施已存在**：`packages/eval` 的 `eval replay`（cli.py → replay.py `run_replay`）+ `packages/story-lifecycle` 的 `testing/harness.py run_real_story(workspace, story_key, prd_path, stages=[...], profile=..., adapter="opencode", headless=True)`。profile 模板：`entry/profiles/eval-replay.yaml`（headless + opencode + confirm:false）。**复用它们，不要新写 pipeline_replay.py**。
- **沙箱机关现成**：`STORY_HOME=<沙箱路径>` 隔离 story.db（replay.py:213 已这么做）；证据目录 = 从 workspace 向上爬找首个含 `.agents/` 或 `AGENTS.md` 的目录取 `<该目录>/story/`（infra/story_paths.py:80）——沙箱 workspace 里放一个 `AGENTS.md` 即截断爬升，证据全落沙箱。
- **无副作用确认**：本体无钉钉 webhook；TAPD 写仅手动 API 触发；git 变更只发生在 build 阶段（且由 spawn 的 AI agent 执行，Python 控制面外）——摘除 build 即免疫。
- **阶段形态**：intake/PRD 生成 = 纯 Python（`orchestrator/service/prd_generator.py generate_prd_from_source(source: StorySourceSnapshot)`）；spec/design = 编排器渲染 prompt + spawn AI CLI（headless 走 opencode）；verify/gate = 纯 Python（`orchestrator/evaluation/unified_gate.py run_unified_verify_gate`，输入含 workspace 下 `.story/done/<key>/<stage>.json` 握手文件）。
- **唯一缺口**：`eval replay` 的 gold 抽取假设需求走过管线（有 evidence_dir/spec.md）。历史需求需要新适配器：TAPD 描述 + story_refs → 回放输入。
- LLM 端点：一律 opencode-go（`STORY_LLM_BASE_URL=https://opencode.ai/zen/go/v1` + `OPENCODE_API_KEY` 注入 `STORY_LLM_API_KEY`，模型 deepseek-v4-flash），禁 DeepSeek 官方端点。

## 1. 铁律

- 输入：`dataset/snapshot_20260805/`（活数据禁用）。hc-all 各 repo 只读（`git -C <repo> diff <merge>^1 <merge>`，不 checkout）。
- 沙箱：`packages/eval/sandbox/`（加 gitignore），其下 `STORY_HOME=sandbox/story_home/`、workspace=`sandbox/ws/<story_key>/`（各放 `AGENTS.md` 截断证据爬升）。**禁止**写原 story.db、`D:/hc-all/story/`、起 8180 server、任何 git 变更。
- 不得修改 story-lifecycle 核心包代码；新代码限 `packages/eval/src/eval/`。`infra/config.py CONFIG_DIR`、`story_evidence_root` 爬升逻辑如碍事，monkeypatch 在 eval 侧做，不改源码。
- 产物只落 `packages/eval/results/` 与沙箱内。

## 2. 样本（20 条，带标签，同 round 1 口径）

从快照 stories_matched.jsonl 选 20 个 story，四类各 5：

| 类别 | 选取规则 | 期望行为 |
|---|---|---|
| A. 干净交付 | merge alignment≥4 且 coverage≥4 | 跑完 + gate 放行 |
| B. 已知 drift | merge alignment≤2（round 1 正样本中 gate 拦截理由最明确的） | gate 拦截且理由命中已知 findings |
| C. 参照物曾缺失 | story_refs 富化的 19 个 story 中挑 | intake/PRD 能利用 story_refs，不再「无参照物」 |
| D. 跨服务需求 | evidence/spec 涉及 ≥2 repo | spec 应拆交付清单（round 1 头号失败模式） |

## 3. 实施步骤

1. **gold 适配器**（`packages/eval/src/eval/replay_extract.py`，新）：从快照取 TAPD name + description（link-only 时用 story_refs 正文替代/拼接）→ 生成 `sandbox/gold/<story_key>/PRD.md`；同时组装该 story 历史交付的 diff（`git diff <merge>^1 <merge>`，多 merge 拼接）备用。
2. **回放 profile**（`sandbox/profiles/replay-nb.yaml`，参照 eval-replay.yaml 裁）：只含 design → verify 两个 stage；`confirm:false`、`execution_mode: headless`、adapter opencode；`artifacts`/`expected_outputs` 契约按无 build 裁剪（注意 profile_loader 强制每 stage ≥1 个文件类 artifact）。
3. **verify 的历史交付注入**：design 完成后、verify 前，把历史 diff 写成 gate 能消费的形式——`.story/done/<key>/design.json`（或对应 stage）的 done_data/files_changed 按真实 merge 的文件清单填充，diff 全文落 `sandbox/ws/<key>/delivery.diff` 并在 done_data 里引用。
4. **驱动**：逐 story 调 `run_real_story`（STORY_HOME 指向沙箱；workspace 用 sandbox/ws/<key>；project registry 喂空或最小注册，绕开 prepare_worktrees 对真仓的假设）。记录每阶段成败、耗时、LLM 调用数（llm_trace 表）、产物路径、gate verdict+findings。单 story 30 分钟熔断。
5. 若整链路驱动在某环节卡死（接口假设不成立），允许降级「分段驱动」（PRD 生成单独调 `generate_prd_from_source`，verify 单独调 `run_unified_verify_gate`），报告中明确标注哪些 story 是分段跑的、卡在哪。

## 4. 度量与产出

`results/pipeline_replay_20260805.md`，版本锚点头（story-lifecycle HEAD + git status 摘要、快照名、LLM 端点、涉及 hc-all repo HEAD）：

1. **健壮性**：完成率（四类分列）、失败点分布（阶段 + 异常）；
2. **产物质量**：生成的 spec 用 judge（ConformanceScore 同款 prompt，参照物 = gold 输入）打 alignment 均分，分类别列；
3. **拦截一致性**：B 类 gate 拦截理由 vs round 1 已知 findings 的命中数；A 类误拦数；
4. **问题清单**：暴露的每个管线缺陷（崩溃、幻觉字段、格式不兼容、隔离泄漏），按严重度排序，附 story id + 现象 + 证据；
5. **沙箱审计**：原 story.db 回放前后 md5 一致、`D:/hc-all/story/` 零写入、hc-all 各 repo `git status` 无变化——三项全过附证据。

## 5. 验收（完成定义）

1. 报告落盘，20 条样本每条有完整记录；沙箱审计三项全过；
2. 拦截一致性可对照明细复算；生成 spec 评分可在沙箱产物复查；
3. `git status`：story-lifecycle 核心包零改动、packages/eval 之外零代码改动；
4. 回复给出：完成率（分类别）、生成 spec 均分、B 类命中数、A 类误拦数、缺陷 top5。

## 6. 后续（不在本任务内）

问题清单 = story-lifecycle 第一批真实改动的输入。改动后同批样本同沙箱 replay，对比 delta，构成完整迭代循环。
