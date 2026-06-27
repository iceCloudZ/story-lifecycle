# M1 验证报告：monorepo 骨架 + 迁移

## 决策回顾

- **宿主**：`story-lifecycle` 仓库改造（方案 C1）
- **monorepo 名**：`dev-flywheel`（保留 `story-lifecycle` 作为 GitHub 仓库名）
- **原 miner 包名**：`agent-transcript-miner` → `story-miner`
- **开源策略**：整体开源
- **hc-all 泛化**：结构迁移优先，硬编码暂时保留（M6 处理）

## 目录结构

```
D:/github/story-lifecycle/
├── README.md                 # monorepo 顶层说明
├── pyproject.toml            # workspace 元数据
├── .gitignore                # monorepo 根级忽略
├── docs/
│   ├── MIGRATION.md          # 迁移方案
│   ├── INTEGRATION.md        # 契约规范
│   └── ADOPTION.md           # 采用清单
└── packages/
    ├── story-lifecycle/      # 原 story-lifecycle 整体移入
    └── story-miner/          # 原 agent-transcript-miner 通过 git subtree 迁入
```

## 历史保留验证

### story-lifecycle

- 迁移方式：在本分支内整体 `git mv` 到 `packages/story-lifecycle/`，Git 识别为 rename。
- 验证命令：
  ```bash
  git log --oneline --follow -5 -- packages/story-lifecycle/src/story_lifecycle/__init__.py
  ```
- 结果：可追溯至 `ba85e7c init: story-lifecycle project skeleton`，历史连续。

### story-miner

- 迁移方式：`git subtree add --prefix=packages/story-miner <agent-transcript-miner> main`
- 验证命令：
  ```bash
  git log --oneline --all --graph | head -10
  git cat-file -p <subtree-merge-commit>
  ```
- 结果：
  - subtree merge commit `ffd616c` 有两个 parent：
    1. `9d9a97a`（monorepo 迁移提交）
    2. `427aff8`（原 agent-transcript-miner 的 HEAD）
  - 原 miner 的提交历史（`427aff8` → `53e49cc`）完整保留在第二 parent 链上。
  - 注：因 subtree 把原仓库根路径映射到 `packages/story-miner/`，直接 `git log -- packages/story-miner/` 只能看到 merge commit；完整历史需用 `git log --all` 或 `--graph` 查看。

## 目录完整性

- `packages/story-lifecycle/` 包含原仓库全部 tracked 内容：`src/`、`tests/`、`docs/`、`frontend/`、`examples/`、`scripts/`、`pyproject.toml`、`uv.lock`、`LICENSE` 等。
- `packages/story-miner/` 包含原 `agent-transcript-miner` 全部内容：`miner/`、`scripts/`、`tests/`、`data/`、`docs/`、`config.json`、`CONTEXT.md`、`README.md` 等。

## 顶层 workspace 骨架

- `README.md`：说明 monorepo 目标、包结构、文档入口。
- `pyproject.toml`：声明 workspace 元数据、`dev` 依赖、pytest 测试路径。
- `.gitignore`：覆盖 Python、虚拟环境、IDE、story 本地数据、运行日志等。
- `docs/MIGRATION.md`、`docs/INTEGRATION.md`、`docs/ADOPTION.md`：从 `agent-transcript-miner` 复制到 monorepo 顶层，作为后续 M2–M6 的执行依据。

## 未完成 / 后续卡

- M1 只完成骨架和历史迁移，不改动代码逻辑。
- M2 将处理子包独立 `pyproject.toml` 和 import 路径调整。
- M3 将落地跨项目契约测试。
- M6 将把 `story-miner` 中的 hc-all 硬编码泛化为 config 驱动。

## 结论

M1 验收通过：
- [x] `packages/story-lifecycle/` 和 `packages/story-miner/` 目录完整
- [x] `story-lifecycle` 历史连续（`git log --follow` 可追溯）
- [x] `story-miner` 原始提交作为 subtree 第二 parent 完整保留
- [x] 顶层 workspace 骨架和迁移文档就位
