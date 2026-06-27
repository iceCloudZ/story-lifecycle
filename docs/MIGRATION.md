# Monorepo 迁移 + 统一知识层方案

> 把 `story-lifecycle` + `agent-transcript-miner` 合并成 monorepo，并统一两者的知识/数据飞轮。
> 执行依据，其他 AI 领卡推进。先读 `CONTEXT.md` / `INTEGRATION.md` / `ADOPTION.md`。

## 1. 背景与决策

两个项目运行时已一体（story-lifecycle 通过 importlib 动态加载 miner 的 provider），但仓库分开是人为切割，导致：
- 跨项目改动要两个 repo 两个 PR，契约容易半边飘
- `.story/knowledge/` 两套机制共处但**不对齐**（story-lifecycle 的 scenarios/indexes/graph vs miner 的 playbooks/by-story）
- 跨项目契约测试难落地

**决策**：合并成 **monorepo**（一个仓库、`packages/` 下独立子包），不硬合并成一个项目。理由：保留 story-lifecycle 的独立开源身份/通用性，同时得到联动零成本。miner 的金融特化是数据/config（非代码本质），泛化后通用。

## 2. 现状盘点

### story-lifecycle 数据飞轮
| 飞轮 | 位置 | 内容 | 性质 |
|---|---|---|---|
| knowledge 模块 | `src/story_lifecycle/knowledge/`(bootstrap/detector/generator/search/stale) | 生成 scenarios(15)/indexes(9)/graph(1) | 静态（代码结构）|
| benchmarks 归因 | `benchmarks/attribution.py` | stage_logs→failure_stage/root_cause | 失败归因 |
| 自身开发飞轮 | `.story/` + `.story-knowledge/` | 用自己管理自己（CR-ISO1/DEV-/FATIGUE 等）| 自举数据 |

### 与 miner 的重叠/互补
| 维度 | story-lifecycle | miner | 关系 |
|---|---|---|---|
| 知识索引 | scenarios/indexes/graph | playbooks/by-story | 互补：静态 vs 动态 |
| 失败分析 | attribution(stage_logs) | failure_mode(transcript) | 重叠，数据源不同 |
| 阶段成本 | stage_logs | stage_cost | 重叠 |
| 复盘 | — | retrospect | miner 独有 |

**关键**：hc-all/.story/knowledge/ 已物理共处（story-lifecycle 填 scenarios类、miner 填 playbooks），只是 schema 不对齐、INDEX 不统一。

## 3. 目标架构

```
<monorepo>/
├── packages/
│   ├── story-lifecycle/   流程引擎 + 静态 knowledge（scenarios/indexes/graph），独立 pip install / 可开源
│   ├── miner/             动态知识（playbooks/failure_mode/retrospect），独立 pip install
│   └── knowledge/   ←     统一知识层（新）：schema 对齐 + 统一 INDEX + failure 合并
├── tests/contracts/       跨项目契约测试（anchors/provider/e2e）
├── .github/workflows/     一个 CI 测全部
└── pyproject/ workspace   共享工具链
```

`.story/knowledge/` 统一一套：scenarios（业务结构）+ playbooks（任务经验）互链，agent 拿"业务结构 + 历史经验"一体。

## 4. 待决策点（阻塞，需用户先定，AI 不能替定）

- [ ] **monorepo 仓库名**（如 dev-platform / story-forge / 用户定）
- [ ] **宿主**：用 miner repo 改造 / 用 story-lifecycle repo 改造 / 全新 repo
- [ ] **story-lifecycle 开源身份**：monorepo 整体开源 / story-lifecycle 子包仍独立发布到 github / miner 不开源
- [ ] **miner hc-all 硬编码泛化范围**（见 M6）：playbook 输出路径 / ws_of 关键词 / constraint·debt 接的 hc-all skill —— 哪些必须泛化、哪些留作默认

## 5. 阶段 1 任务卡（快：骨架 + 迁移 + 契约）

### M1 monorepo 骨架 + 迁移（保 history）
- 做：建 packages/ 结构；`git subtree`/`git filter-repo` 把 story-lifecycle 和 miner 迁入各自 packages/（**保留 git history**）；顶层 workspace 配置
- 验收：`git log` 在各子包能追溯到原 repo history；两子包目录结构完整
- 约束：history 保留是硬要求（story-lifecycle 开源 history 不能丢）

### M2 子包 pyproject + import 调整
- 做：各子包独立 `pyproject.toml`（能 `pip install -e packages/story-lifecycle` / `packages/miner`）；调整 import path（`story_lifecycle.*` / `miner.*` 保持或改）；跑通两子包各自现有测试
- 验收：两子包独立 pip install 成功；原测试全绿（story-lifecycle 17、miner 6 等）
- 约束：import 改动要全量过测试

### M3 跨项目契约测试
- 做：`tests/contracts/` 放 4 契约测试（anchors 写读 / provider 协议 / done_cmd→retrospect / store-link schema 解耦）；committed fixtures + 双向断言
- 验收：CI 一键测全链路；story-lifecycle 改 anchors → 测试失败；miner 改 read → 测试失败
- 约束：见 INTEGRATION.md 契约清单

## 6. 阶段 2 任务卡（渐进：统一知识层）

### M4 统一知识层 schema 设计
- 做：设计统一知识包 schema（scenarios + playbooks 对齐字段：id/type/title/触发/必看文件/角色/来源 static|dynamic）；设计统一 INDEX（静态场景 + 动态任务上下文 互链）；写 `packages/knowledge/schema.md`
- 验收：schema 能容纳现有 scenarios(15) + playbooks(19) 不丢信息；INDEX 设计评审过
- 约束：scenarios 的业务结构语义 + playbooks 的行为经验语义都要保留

### M5 知识层统一实现
- 做：`packages/knowledge/` 实现统一生成器/INDEX/failure 合并（attribution + failure_mode → 统一失败知识）；迁移现有 scenarios+playbooks 到新 schema；hc-all/.story/knowledge/ 重建为统一格式
- 验收：统一 INDEX 覆盖 scenarios+playbooks；failure 知识合并无重复；agent 能一次拿到"业务结构+任务经验"
- 约束：渐进，不破坏现有 skill 引用（playbook 文件名稳定）

### M6 miner hc-all 硬编码泛化
- 做：playbook 输出路径改 config 驱动（不写死 hc-all/.story/knowledge）；ws_of 关键词改 config；constraint/debt 接的 hc-all skill 改成可配置钩子
- 验收：换 config 能分析非 hc-all 项目；hc-all 作为默认配置仍 work
- 约束：保持 miner 核心通用，hc-all 只是默认场景

## 7. 风险清单

1. **story-lifecycle 通用性**：knowledge 模块通用，迁移后不能 hc-all 特化（M6 配合）
2. **import path 改动**：两项目所有 import 要过测试（M2）
3. **git history 保留**：subtree/filter-repo 有坑，story-lifecycle 开源 history 必须连续（M1）
4. **开源身份**：monorepo 开源则 miner 代码也公开（hc-all 特化逻辑要清理，M6）
5. **知识层统一是设计重活**：schema 要兼容两种语义，别急于一次到位（M4 先设计、M5 渐进）

## 8. 派发 prompt（复制到其他窗口）

### M1 monorepo 骨架 + 迁移（先做，阻塞后续）
```
实施 monorepo 迁移的 M1。先读 D:/github/agent-transcript-miner/docs/MIGRATION.md 的 [M1 卡] + 第4节待决策点。
【前置】需用户先定：monorepo 仓库名 / 宿主（miner 或 story-lifecycle repo 改造或全新）/ story-lifecycle 开源身份。未定则停下问。
做：packages/ 结构；git subtree 或 filter-repo 把 D:/github/story-lifecycle 和 D:/github/agent-transcript-miner 迁入各自 packages/（保留 git history）；顶层 workspace。
验收按 M1：各子包 git log 追溯到原 history；目录完整。写 packages/migration-m1-verify.md。
```

### M3 跨项目契约测试（M1/M2 后）
```
实施 monorepo 的 M3（跨项目契约测试）。先读 docs/MIGRATION.md [M3 卡] + docs/INTEGRATION.md（4 契约清单）。
做：tests/contracts/ 放 4 契约测试（anchors 写读 / provider 协议 / done_cmd→retrospect / store-link schema 解耦），committed fixtures + 双向断言。
验收按 M3：CI 测全链路；改一边破另一边时测试失败。写 packages/migration-m3-verify.md。
```

（M2/M4/M5/M6 类似，按卡执行，先读 MIGRATION.md 对应卡 + 第4节决策点）

## 9. 验收（主窗口）
各卡完成后读 `packages/migration-mN-verify.md` + 复核硬指标：M1 history 连续、M2 测试全绿、M3 契约测试真能拦 breaking change、M5 统一 INDEX 覆盖 scenarios+playbooks、M6 换 config 能分析非 hc-all。
