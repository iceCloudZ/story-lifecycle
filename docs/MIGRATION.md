# Monorepo 迁移 + 统一知识层方案

> 把 `story-lifecycle` + `agent-transcript-miner` 合并成 monorepo，并统一两者的知识/数据飞轮。
> **M1–M6 已完成。** 本文档保留为历史决策记录与验收依据。

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
│   ├── story-miner/       动态知识（playbooks/failure_mode/retrospect），独立 pip install
│   └── knowledge/   ←     统一知识层（新）：schema 对齐 + 统一 INDEX + failure 合并
├── tests/contracts/       跨项目契约测试（anchors/provider/done→retrospect/store-link）
├── .github/workflows/     一个 CI 测全部
└── pyproject.toml         workspace 共享工具链
```

`.story/knowledge/` 统一一套：scenarios（业务结构）+ playbooks（任务经验）互链，agent 拿"业务结构 + 历史经验"一体。

## 4. 待决策点（已落地）

- [x] **monorepo 仓库名**：沿用 `story-lifecycle`（顶层 README 标题为 `dev-flywheel`）。
- [x] **宿主**：在 `D:/github/story-lifecycle` 基础上扩展为 monorepo。
- [x] **story-lifecycle 开源身份**：monorepo 整体开源，子包独立 pip install。
- [x] **miner hc-all 硬编码泛化范围**（M6）：playbook 输出路径改 config 驱动；`ws_of` 关键词保持默认但可配置；skill 钩子改为按工作区配置。

## 5. 阶段 1 任务卡（快：骨架 + 迁移 + 契约）

### M1 monorepo 骨架 + 迁移（保 history）`[已完成]`
- 做：建 `packages/` 结构；保留两子包 git history；顶层 `pyproject.toml` workspace 配置。
- 状态：`packages/story-lifecycle/`、`packages/story-miner/`、`packages/knowledge/`、`tests/contracts/` 均就位。
- 验收：`git log` 在各子包能追溯到原 repo history；两子包目录结构完整。
- 约束：history 保留是硬要求（story-lifecycle 开源 history 不能丢）。

### M2 子包 pyproject + import 调整 `[已完成]`
- 做：各子包独立 `pyproject.toml`（`pip install -e packages/story-lifecycle`、`pip install -e packages/story-miner`）；`story_lifecycle.*` / `miner.*` import 保留；统一 knowledge 包共享 schema。
- 验收：两子包独立 pip install 成功；全量测试 `660 passed, 2 skipped`。
- 约束：import 改动要全量过测试。

### M3 跨项目契约测试 `[已完成]`
- 做：`tests/contracts/` 已覆盖 anchors 写读 / provider 协议 / done_cmd→retrospect / store-link schema。
- 验收：CI 一键测全链路；任何一边破坏契约即测试失败。
- 约束：见 `docs/INTEGRATION.md` 契约清单。

## 6. 阶段 2 任务卡（渐进：统一知识层）

### M4 统一知识层 schema 设计 `[已完成]`
- 做：`packages/knowledge/schema.md` + `packages/knowledge/src/knowledge/schema.py` 定义统一 INDEX；scenarios 与 playbooks 字段对齐（id/type/title/触发/必看文件/角色/来源 static|dynamic）。
- 验收：schema 容纳现有 scenarios + playbooks 不丢信息；统一 INDEX 覆盖两者。
- 约束：scenarios 的业务结构语义 + playbooks 的行为经验语义都要保留。

### M5 知识层统一实现 `[已完成]`
- 做：`packages/knowledge/` 实现统一生成器、INDEX、检索；`failure_mode` 与 `attribution` 合并为统一失败知识；现有 scenarios + playbooks 迁移到新 schema。
- 验收：统一 INDEX 覆盖 scenarios+playbooks；failure 知识合并无重复；agent 能一次拿到"业务结构+任务经验"。
- 约束：渐进，不破坏现有 skill 引用（playbook 文件名稳定）。

### M6 miner hc-all 硬编码泛化 `[已完成]`
- 做：playbook 输出路径改 config 驱动；`ws_of` 关键词保持默认但可扩展；分析脚本通过 `config.WORKSPACES` 驱动，不再硬编码 hc-all。
- 验收：换 config 能分析非 hc-all 项目；hc-all 作为默认配置仍 work。
- 约束：保持 miner 核心通用，hc-all 只是默认场景。

## 7. 风险清单

1. **story-lifecycle 通用性**：knowledge 模块通用，迁移后不能 hc-all 特化（M6 配合）
2. **import path 改动**：两项目所有 import 要过测试（M2）
3. **git history 保留**：subtree/filter-repo 有坑，story-lifecycle 开源 history 必须连续（M1）
4. **开源身份**：monorepo 开源则 miner 代码也公开（hc-all 特化逻辑要清理，M6）
5. **知识层统一是设计重活**：schema 要兼容两种语义，别急于一次到位（M4 先设计、M5 渐进）

## 8. 派发 prompt（已归档，不再派发）

M1–M6 已完成，以下 prompt 仅作历史参考：

### M1 monorepo 骨架 + 迁移（历史）
```
实施 monorepo 迁移的 M1。先读 D:/github/story-lifecycle/docs/MIGRATION.md 的 [M1 卡] + 第4节待决策点。
做：packages/ 结构；git subtree 或 filter-repo 把 story-lifecycle 和 agent-transcript-miner 迁入各自 packages/（保留 git history）；顶层 workspace。
验收按 M1：各子包 git log 追溯到原 history；目录完整。写 packages/migration-m1-verify.md。
```

### M3 跨项目契约测试（历史）
```
实施 monorepo 的 M3（跨项目契约测试）。先读 docs/MIGRATION.md [M3 卡] + docs/INTEGRATION.md（4 契约清单）。
做：tests/contracts/ 放 4 契约测试（anchors 写读 / provider 协议 / done_cmd→retrospect / store-link schema 解耦），committed fixtures + 双向断言。
验收按 M3：CI 测全链路；改一边破另一边时测试失败。写 packages/migration-m3-verify.md。
```

## 9. 验收结论

- M1–M6 全部完成，monorepo 结构稳定。
- 全量测试：`660 passed, 2 skipped`。
- 关键产物：
  - `packages/story-lifecycle/`、`packages/story-miner/`、`packages/knowledge/`
  - `tests/contracts/`
  - `packages/story-miner/scripts/out/i2-i4-verify.md`（I1–I4 集成验证）
